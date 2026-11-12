"""CB Insights sitemap index.

CB Insights publishes a sitemap index at

    https://www.cbinsights.com/sitemap/sitemap_master.xml

which robots.txt names explicitly, so reading it is invited rather than
tolerated. It fans out to ~1,279 child sitemaps in a few families:

    company        638 files, ~4,600 URLs each   ~2.9M companies
    investor        64 files, ~3,000 URLs each   ~195k investors
    compare_page   568 files                     comparison landing pages
    content         ~9 files                     posts, pages, research, authors

Two stages, deliberately separated by cost:

  * `survey()`  — one request. Reads the master and reports every family and
    its file count. Cheap enough to run on a page load.
  * `refresh()` — walks the child sitemaps of the selected families and
    stores every entity slug. ~13 minutes for company + investor, so it runs
    as a background job with progress.

Deep indexing defaults to company and investor: those name real entities the
pipeline can resolve against. `compare_page` is 568 files of marketing
permutations (`/compare/a-vs-b`) and is off by default.

What this index is: a directory of which companies and investors exist and at
what URL. It stores no page content. Note that CB Insights' robots.txt
disallows several named AI crawlers from `/company/` and `/investor/` page
bodies while leaving them open to `User-agent: *` — so fetching those pages
is a separate decision from building this index, and is not done here.
"""
from __future__ import annotations

import os
import re
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx

MASTER_URL = "https://www.cbinsights.com/sitemap/sitemap_master.xml"
REQUEST_TIMEOUT = 45.0
USER_AGENT = "StartlineBot/0.1 (+https://startline.vip/bot; company-card indexing)"
DEFAULT_FAMILIES = ("company", "investor")
INSERT_BATCH = 5_000

_LOC = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.I)

DDL = """
-- The profile URL is not stored: it is exactly
-- https://www.cbinsights.com/<kind>/<slug>, and at ~3M rows that column costs
-- roughly 200MB to hold a value that is free to derive. See entity_url().
CREATE TABLE IF NOT EXISTS cbi_entity (
    slug TEXT NOT NULL,
    kind TEXT NOT NULL,              -- 'company' | 'investor'
    norm TEXT NOT NULL,              -- lowercase alphanumeric match key
    PRIMARY KEY (kind, slug)
);
CREATE INDEX IF NOT EXISTS idx_cbi_norm ON cbi_entity(norm);
CREATE TABLE IF NOT EXISTS cbi_sitemap (
    url TEXT PRIMARY KEY,
    family TEXT NOT NULL,
    urls INTEGER,
    status INTEGER,
    fetched_at TEXT
);
CREATE TABLE IF NOT EXISTS cbi_meta (key TEXT PRIMARY KEY, value TEXT);
"""

# Same normalisation as the Crunchbase index, so a name resolves consistently
# across both directories.
_SUFFIXES = re.compile(
    r"\b(inc|incorporated|corp|corporation|co|company|ltd|limited|llc|llp|plc|"
    r"gmbh|ag|sa|bv|nv|oy|ab|as|pte|pty|kk|kabushiki|holdings?|group|labs?|"
    r"technologies|technology|tech|solutions|systems|ventures|partners)\b",
    re.I,
)


def normalize(name: str) -> str:
    text = re.sub(r"[^\w\s-]", " ", (name or "").lower())
    text = _SUFFIXES.sub(" ", text)
    return re.sub(r"[^a-z0-9]", "", text)


def family_of(url: str) -> str:
    """Classify a child sitemap URL into its family."""
    tail = url.rsplit("/", 1)[-1]
    match = re.match(r"sitemap_([a-z_]+?)_?\d*\.xml", tail)
    if match:
        return match.group(1)
    match = re.match(r"([a-z-]+)-sitemap\.xml", tail)
    if match:
        return match.group(1).replace("-", "_")
    return "content"


def entity_url(kind: str, slug: str) -> str:
    return f"https://www.cbinsights.com/{kind}/{slug}"


def entity_of(url: str) -> tuple[str, str] | None:
    """('company'|'investor', slug) for an entity URL, else None."""
    parts = url.split("/")
    if len(parts) < 5 or parts[3] not in ("company", "investor"):
        return None
    slug = parts[4].strip()
    return (parts[3], slug) if slug else None


@dataclass
class Survey:
    families: dict[str, int] = field(default_factory=dict)
    total_sitemaps: int = 0
    fetched_at: str = ""

    def to_dict(self) -> dict:
        return {"families": self.families, "total_sitemaps": self.total_sitemaps,
                "fetched_at": self.fetched_at}


class CBInsightsSitemapIndex:
    def __init__(self, db_path: str | None = None):
        if db_path is None:
            root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            os.makedirs(os.path.join(root, "data"), exist_ok=True)
            db_path = os.path.join(root, "data", "cbinsights.db")
        self.db_path = db_path
        with sqlite3.connect(self.db_path) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(DDL)
            # An index built before the url column was dropped still has it,
            # and its NOT NULL would reject every new insert. This is a
            # rebuildable cache, so replacing the table is cheaper and safer
            # than migrating rows.
            columns = {row[1] for row in db.execute("PRAGMA table_info(cbi_entity)")}
            if "url" in columns:
                db.execute("DROP TABLE cbi_entity")
                db.executescript(DDL)
                db.execute("DELETE FROM cbi_meta WHERE key IN "
                           "('indexed_at','indexed_families')")
                db.execute("UPDATE cbi_sitemap SET fetched_at=NULL, urls=NULL")
                db.commit()

    # -- helpers -------------------------------------------------------------

    def _client(self) -> httpx.Client:
        return httpx.Client(timeout=REQUEST_TIMEOUT, follow_redirects=True,
                            headers={"User-Agent": USER_AGENT})

    def _set_meta(self, db, key: str, value: str) -> None:
        db.execute("INSERT INTO cbi_meta (key,value) VALUES (?,?) "
                   "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    def meta(self, key: str) -> str | None:
        with sqlite3.connect(self.db_path) as db:
            row = db.execute("SELECT value FROM cbi_meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    # -- stage 1: survey the master (one request) ----------------------------

    def survey(self, force: bool = False) -> Survey:
        """Read the master sitemap and report the family breakdown."""
        if not force:
            cached = self.meta("survey")
            if cached:
                import json
                data = json.loads(cached)
                return Survey(data["families"], data["total_sitemaps"], data["fetched_at"])

        with self._client() as client:
            response = client.get(MASTER_URL)
            response.raise_for_status()
        children = _LOC.findall(response.text)

        families: dict[str, int] = {}
        now = datetime.now(timezone.utc).isoformat()
        with sqlite3.connect(self.db_path) as db:
            db.executescript(DDL)
            rows = []
            for url in children:
                fam = family_of(url)
                families[fam] = families.get(fam, 0) + 1
                rows.append((url, fam))
            db.executemany(
                "INSERT INTO cbi_sitemap (url,family) VALUES (?,?) "
                "ON CONFLICT(url) DO UPDATE SET family=excluded.family", rows)
            import json
            survey = Survey(dict(sorted(families.items(), key=lambda kv: -kv[1])),
                            len(children), now)
            self._set_meta(db, "survey", json.dumps(survey.to_dict()))
            db.commit()
        return survey

    def child_sitemaps(self, families: tuple[str, ...]) -> list[tuple[str, str]]:
        marks = ",".join("?" for _ in families)
        with sqlite3.connect(self.db_path) as db:
            return db.execute(
                f"SELECT url,family FROM cbi_sitemap WHERE family IN ({marks}) "
                "ORDER BY family, url", families).fetchall()

    # -- stage 2: deep index -------------------------------------------------

    def refresh(self, families: tuple[str, ...] = DEFAULT_FAMILIES,
                progress=None, cancel=None, limit: int | None = None) -> dict:
        """Walk the child sitemaps of `families`, storing every entity slug.

        `progress(done, total, note)` is called after each child sitemap;
        `cancel()` returning True aborts between files, keeping what was
        already stored — the index is additive, so a cancelled run is a
        partial index rather than a corrupt one.
        """
        self.survey()
        targets = self.child_sitemaps(families)
        if limit:
            targets = targets[:limit]
        total = len(targets)
        started = time.time()
        done = failed = stored = 0

        with self._client() as client, sqlite3.connect(self.db_path) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=NORMAL")
            db.executescript(DDL)
            pending: list[tuple] = []

            def flush() -> None:
                nonlocal pending
                if pending:
                    db.executemany(
                        "INSERT INTO cbi_entity (slug,kind,norm) VALUES (?,?,?) "
                        "ON CONFLICT(kind,slug) DO UPDATE SET norm=excluded.norm",
                        pending)
                    db.commit()
                    pending = []

            for url, family in targets:
                if cancel and cancel():
                    flush()
                    break
                try:
                    response = client.get(url)
                    status = response.status_code
                    entities = 0
                    if status == 200:
                        for loc in _LOC.findall(response.text):
                            found = entity_of(loc)
                            if found is None:
                                continue
                            kind, slug = found
                            pending.append((slug, kind, normalize(slug)))
                            entities += 1
                            stored += 1
                    else:
                        failed += 1
                except httpx.HTTPError as exc:
                    status, entities = 0, 0
                    failed += 1
                    if progress:
                        progress(done, total, f"{url.rsplit('/',1)[-1]}: {type(exc).__name__}")
                db.execute(
                    "INSERT INTO cbi_sitemap (url,family,urls,status,fetched_at) "
                    "VALUES (?,?,?,?,?) ON CONFLICT(url) DO UPDATE SET "
                    "urls=excluded.urls, status=excluded.status, "
                    "fetched_at=excluded.fetched_at",
                    (url, family, entities, status,
                     datetime.now(timezone.utc).isoformat()))
                if len(pending) >= INSERT_BATCH:
                    flush()
                done += 1
                if progress:
                    progress(done, total, url.rsplit("/", 1)[-1])
            flush()
            self._set_meta(db, "indexed_at", datetime.now(timezone.utc).isoformat())
            self._set_meta(db, "indexed_families", ",".join(families))
            db.commit()

        self.compact()
        return {"sitemaps_read": done, "sitemaps_total": total, "failed": failed,
                "entities_seen": stored, "seconds": round(time.time() - started, 1),
                **self.counts()}

    def compact(self) -> None:
        """Fold the WAL back and reclaim free pages.

        A full walk writes several hundred MB through the WAL and leaves free
        pages behind; without this the file on disk can be several times the
        size of the data it holds. Runs outside any transaction, as VACUUM
        requires.
        """
        db = sqlite3.connect(self.db_path, isolation_level=None)
        try:
            db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            db.execute("VACUUM")
        except sqlite3.OperationalError:
            pass                          # another reader holds the file; harmless
        finally:
            db.close()

    # -- read ----------------------------------------------------------------

    def counts(self) -> dict:
        with sqlite3.connect(self.db_path) as db:
            try:
                rows = db.execute(
                    "SELECT kind, COUNT(*) FROM cbi_entity GROUP BY kind").fetchall()
            except sqlite3.OperationalError:
                return {"companies": 0, "investors": 0}
        by_kind = dict(rows)
        return {"companies": by_kind.get("company", 0),
                "investors": by_kind.get("investor", 0)}

    def lookup(self, name: str, kind: str = "company") -> dict | None:
        """Exact normalized-name match. Deliberately not fuzzy: a wrong match
        here becomes a verified fact about a different company."""
        key = normalize(name)
        if not key:
            return None
        with sqlite3.connect(self.db_path) as db:
            db.row_factory = sqlite3.Row
            row = db.execute(
                "SELECT * FROM cbi_entity WHERE norm=? AND kind=? "
                "ORDER BY length(slug) LIMIT 1", (key, kind)).fetchone()
        if row is None:
            return None
        found = dict(row)
        found["url"] = entity_url(found["kind"], found["slug"])
        return found

    def search(self, term: str, kind: str | None = None, limit: int = 50) -> list[dict]:
        """Prefix search for the console's browse table."""
        key = normalize(term)
        if not key:
            return []
        sql = "SELECT * FROM cbi_entity WHERE norm LIKE ?"
        args: list = [key + "%"]
        if kind:
            sql += " AND kind=?"
            args.append(kind)
        sql += " ORDER BY length(slug) LIMIT ?"
        args.append(max(1, min(limit, 200)))
        with sqlite3.connect(self.db_path) as db:
            db.row_factory = sqlite3.Row
            rows = [dict(r) for r in db.execute(sql, args).fetchall()]
        for row in rows:
            row["url"] = entity_url(row["kind"], row["slug"])
        return rows

    def browse(self, offset: int = 0, limit: int = 50) -> list[dict]:
        with sqlite3.connect(self.db_path) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute(
                "SELECT * FROM cbi_entity ORDER BY kind, slug LIMIT ? OFFSET ?",
                (max(1, min(limit, 200)), max(0, offset))).fetchall()
        return [{"name": r["slug"], "kind": r["kind"], "detail": "",
                 "url": entity_url(r["kind"], r["slug"])} for r in rows]

    def batch_slice(self, offset: int, limit: int) -> list[dict]:
        """A fixed slice of the index, ordered by primary key.

        Deliberately not `browse()`: that sorts for readability, and any sort
        whose key can change would make batch 5 mean different listings on
        different days. Batch numbering has to be anchored to something
        immutable.
        """
        with sqlite3.connect(self.db_path) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute(
                "SELECT * FROM cbi_entity ORDER BY kind, slug LIMIT ? OFFSET ?",
                (max(1, limit), max(0, offset))).fetchall()
        return [{"ref": r["slug"], "name": r["slug"], "kind": r["kind"],
                 "url": entity_url(r["kind"], r["slug"])} for r in rows]

    def total(self) -> int:
        with sqlite3.connect(self.db_path) as db:
            try:
                return db.execute("SELECT COUNT(*) FROM cbi_entity").fetchone()[0]
            except sqlite3.OperationalError:
                return 0

    def progress_snapshot(self) -> dict:
        """Per-family deep-index coverage, for the console."""
        with sqlite3.connect(self.db_path) as db:
            rows = db.execute(
                "SELECT family, COUNT(*) AS files, "
                "SUM(CASE WHEN fetched_at IS NOT NULL THEN 1 ELSE 0 END) AS fetched, "
                "COALESCE(SUM(urls),0) AS urls FROM cbi_sitemap GROUP BY family "
                "ORDER BY files DESC").fetchall()
        return {"families": [
            {"family": r[0], "files": r[1], "fetched": r[2], "urls": r[3]}
            for r in rows]}


if __name__ == "__main__":       # python -m crawlers.cbinsights_sitemap
    index = CBInsightsSitemapIndex()
    print(index.survey(force=True).to_dict())
