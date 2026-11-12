"""The crawl ladder, wired into the agent as its official-site adapter.

`LadderSiteAdapter` replaces the agent's built-in crawler, so a pipeline tick
harvests pages through Scrapy → Crawlee → Playwright instead of a
single requests+BeautifulSoup pass. Everything downstream is untouched: it
still returns `ScrapedDoc`s, and the parent class still turns each page into
cleaned text plus the DISCOVERED ASSETS appendix that the cleaner's evidence
gate depends on.

It also holds the state that closes the loop with verification:

    ladder_state(domain) = floor_tier + want_fields

When the Agent OS finds a company card missing `contact_email`, it writes that
field into `want_fields` and raises `floor_tier` above whatever tier last ran.
The next crawl therefore starts higher up the ladder and keeps climbing until a
harvest actually carries the missing signal. Per-run results are recorded here
too, so the UI can show which crawler produced a company's pages and why it had
to escalate.
"""

from __future__ import annotations

import json
import hashlib
import os
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlparse

from . import scrape_ladder as ladder

import startline_agent  # noqa: F401  (puts the agent modules on sys.path)
from orchestrator import ScrapedDoc
from site_adapter import OfficialSiteAdapter

DDL = """
CREATE TABLE IF NOT EXISTS ladder_state (
    domain TEXT PRIMARY KEY,
    floor_tier TEXT NOT NULL DEFAULT 'scrapy',
    want_fields TEXT,                 -- JSON list of fields the card still needs
    last_tier TEXT,                   -- tier whose harvest was kept
    last_ok INTEGER,                  -- did it satisfy the gate?
    last_pages INTEGER,
    last_chars INTEGER,
    last_missing TEXT,                -- JSON list of signals still unanswered
    last_attempts TEXT,               -- JSON list of per-tier attempts
    last_run_at TEXT,
    last_content_hash TEXT,
    transport_stalled INTEGER NOT NULL DEFAULT 0,
    escalations INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS url_frontier (
    domain TEXT NOT NULL,
    url TEXT NOT NULL,
    source_type TEXT NOT NULL DEFAULT 'official_site',
    discovered_from TEXT,
    discovery_method TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending',
    last_tier TEXT,
    last_error TEXT,
    content_hash TEXT,
    discovered_at TEXT NOT NULL,
    last_crawled_at TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_retry_at TEXT,
    claimed_at TEXT,
    terminal_reason TEXT,
    PRIMARY KEY (domain, url)
);
CREATE INDEX IF NOT EXISTS idx_url_frontier_pending
ON url_frontier(domain, status, priority DESC);
"""

_lock = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def bare_domain(value: str) -> str:
    v = (value or "").strip().lower()
    if "://" in v:
        v = urlparse(v).netloc
    return v.split("/")[0].removeprefix("www.")


# ---------------------------------------------------------------------------
# state store (its own connection on the agent's SQLite file)
# ---------------------------------------------------------------------------

class LadderStore:
    def __init__(self, db_path: str):
        self.db = sqlite3.connect(db_path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=15000")
        self.db.executescript(DDL)
        columns = {row["name"] for row in self.db.execute(
            "PRAGMA table_info(url_frontier)")}
        for name, ddl in (
            ("next_retry_at", "TEXT"),
            ("claimed_at", "TEXT"),
            ("terminal_reason", "TEXT"),
        ):
            if name not in columns:
                self.db.execute(f"ALTER TABLE url_frontier ADD COLUMN {name} {ddl}")
        state_columns = {row["name"] for row in self.db.execute(
            "PRAGMA table_info(ladder_state)")}
        for name, ddl in (("last_content_hash", "TEXT"),
                          ("transport_stalled", "INTEGER NOT NULL DEFAULT 0")):
            if name not in state_columns:
                self.db.execute(f"ALTER TABLE ladder_state ADD COLUMN {name} {ddl}")
        active_tiers = tuple(ladder.active_order()) or tuple(ladder.DEFAULT_ORDER)
        marks = ",".join("?" for _ in active_tiers)
        self.db.execute(
            f"UPDATE ladder_state SET floor_tier=? WHERE floor_tier NOT IN ({marks})",
            (active_tiers[0], *active_tiers),
        )
        self.db.execute(
            f"UPDATE ladder_state SET last_tier=NULL,transport_stalled=0 "
            f"WHERE last_tier IS NOT NULL AND last_tier NOT IN ({marks})",
            active_tiers,
        )
        self.db.commit()

    def get(self, domain: str) -> dict:
        domain = bare_domain(domain)
        with _lock:
            row = self.db.execute("SELECT * FROM ladder_state WHERE domain=?",
                                  (domain,)).fetchone()
        if row is None:
            return {"domain": domain, "floor_tier": ladder.TIERS[0],
                    "want_fields": [], "escalations": 0}
        d = dict(row)
        d["want_fields"] = json.loads(d.get("want_fields") or "[]")
        d["last_missing"] = json.loads(d.get("last_missing") or "[]")
        d["last_attempts"] = json.loads(d.get("last_attempts") or "[]")
        d["last_ok"] = bool(d.get("last_ok"))
        return d

    def set_target(self, domain: str, floor_tier: str, want_fields: list[str],
                   bump_escalation: bool = True) -> None:
        """Point the next crawl of this site at a tier and a set of fields."""
        domain = bare_domain(domain)
        with _lock:
            self.db.execute(
                "INSERT INTO ladder_state (domain, floor_tier, want_fields, escalations) "
                "VALUES (?,?,?,?) ON CONFLICT(domain) DO UPDATE SET "
                "floor_tier=excluded.floor_tier, want_fields=excluded.want_fields, "
                "escalations=ladder_state.escalations + ?",
                (domain, floor_tier, json.dumps(want_fields),
                 1 if bump_escalation else 0, 1 if bump_escalation else 0))
            self.db.commit()

    def record_run(self, domain: str, result: ladder.LadderResult) -> None:
        domain = bare_domain(domain)
        with _lock:
            previous = self.db.execute(
                "SELECT last_tier,last_content_hash FROM ladder_state WHERE domain=?",
                (domain,),
            ).fetchone()
            content_blob = "\n".join(sorted(
                " ".join(page.text.split()) for page in result.pages if page.text
            ))
            content_hash = (hashlib.sha256(content_blob.encode("utf-8")).hexdigest()
                            if content_blob else None)
            stalled = int(bool(
                previous and content_hash and previous["last_content_hash"] == content_hash
                and previous["last_tier"] and result.tier
                and previous["last_tier"] != result.tier
            ))
            self.db.execute(
                "INSERT INTO ladder_state (domain, floor_tier, last_tier, last_ok, "
                " last_pages, last_chars, last_missing, last_attempts, last_run_at,"
                " last_content_hash,transport_stalled) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(domain) DO UPDATE SET "
                "last_tier=excluded.last_tier, last_ok=excluded.last_ok, "
                "last_pages=excluded.last_pages, last_chars=excluded.last_chars, "
                "last_missing=excluded.last_missing, "
                "last_attempts=excluded.last_attempts, last_run_at=excluded.last_run_at,"
                "last_content_hash=excluded.last_content_hash,"
                "transport_stalled=excluded.transport_stalled",
                (domain, ladder.TIERS[0], result.tier, int(result.satisfied),
                 len(result.pages), sum(len(p.text) for p in result.pages),
                 json.dumps(result.missing_signals),
                 json.dumps([a.to_dict() for a in result.attempts]), _now(),
                 content_hash, stalled))
            self.db.commit()

    def reset(self, domain: str) -> None:
        """Back to the cheapest tier with no field targets (a clean re-crawl).

        ``last_tier`` and ``escalations`` have to go too. Escalation decides the
        next tier from ``last_tier``, and refuses to climb past a spent
        ``escalations`` budget — leaving either behind means a "reset" domain is
        still treated as maxed out and never gets re-crawled.
        """
        domain = bare_domain(domain)
        with _lock:
            self.db.execute(
                "UPDATE ladder_state SET floor_tier=?, want_fields='[]',"
                " last_tier=NULL,last_content_hash=NULL,transport_stalled=0,"
                " escalations=0 WHERE domain=?",
                (ladder.TIERS[0], domain))
            self.db.commit()

    def all(self) -> dict[str, dict]:
        with _lock:
            rows = self.db.execute("SELECT * FROM ladder_state").fetchall()
        out = {}
        for r in rows:
            d = dict(r)
            d["want_fields"] = json.loads(d.get("want_fields") or "[]")
            d["last_missing"] = json.loads(d.get("last_missing") or "[]")
            d["last_attempts"] = json.loads(d.get("last_attempts") or "[]")
            d["last_ok"] = bool(d.get("last_ok"))
            out[d["domain"]] = d
        return out

    def add_urls(self, domain: str, urls) -> int:
        domain = bare_domain(domain)
        added = 0
        company_budget = max(1, min(int(os.environ.get(
            "STARTLINE_CRAWL_COMPANY_URL_BUDGET", "500")), 5000))
        with _lock:
            active_count = self.db.execute(
                "SELECT COUNT(*) AS n FROM url_frontier WHERE domain=? "
                "AND status IN ('pending','retry','fetching')", (domain,)
            ).fetchone()["n"]
            for item in urls:
                status = "pending" if active_count < company_budget else "deferred_budget"
                cur = self.db.execute(
                    "INSERT OR IGNORE INTO url_frontier "
                    "(domain,url,discovered_from,discovery_method,priority,status,discovered_at) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (domain, item.url, item.discovered_from, item.method,
                     item.priority, status, _now()))
                added += cur.rowcount
                if cur.rowcount and status == "pending":
                    active_count += 1
                self.db.execute(
                    "UPDATE url_frontier SET priority=max(priority,?) "
                    "WHERE domain=? AND url=?", (item.priority, domain, item.url))
            self.db.commit()
        return added

    def _maintain_frontier_locked(self, domain: str) -> dict:
        """Recover abandoned claims and fill the active URL-budget window."""
        stale_seconds = max(30, min(int(os.environ.get(
            "STARTLINE_URL_CLAIM_TIMEOUT_SECONDS", "300")), 86400))
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=stale_seconds)).isoformat()
        recovered = self.db.execute(
            "UPDATE url_frontier SET status='pending',claimed_at=NULL," 
            "last_error='abandoned fetch recovered' WHERE domain=? "
            "AND status='fetching' AND (claimed_at IS NULL OR claimed_at<?)",
            (domain, cutoff),
        ).rowcount
        budget = max(1, min(int(os.environ.get(
            "STARTLINE_CRAWL_COMPANY_URL_BUDGET", "500")), 5000))
        active = self.db.execute(
            "SELECT COUNT(*) AS n FROM url_frontier WHERE domain=? "
            "AND status IN ('pending','retry','fetching')", (domain,),
        ).fetchone()["n"]
        slots = max(0, budget - active)
        promoted = 0
        if slots:
            rows = self.db.execute(
                "SELECT url FROM url_frontier WHERE domain=? "
                "AND status='deferred_budget' ORDER BY priority DESC, discovered_at LIMIT ?",
                (domain, slots),
            ).fetchall()
            if rows:
                self.db.executemany(
                    "UPDATE url_frontier SET status='pending' WHERE domain=? AND url=?",
                    [(domain, row["url"]) for row in rows],
                )
                promoted = len(rows)
        self.db.commit()
        return {"recovered": recovered, "promoted": promoted}

    def maintain_frontier(self, domain: str) -> dict:
        domain = bare_domain(domain)
        with _lock:
            return self._maintain_frontier_locked(domain)

    def pending_urls(self, domain: str, limit: int) -> list[str]:
        """Preview due frontier work without claiming it."""
        domain = bare_domain(domain)
        with _lock:
            self._maintain_frontier_locked(domain)
            rows = self.db.execute(
                "SELECT url FROM url_frontier WHERE domain=? AND (status='pending' OR "
                "(status='retry' AND (next_retry_at IS NULL OR next_retry_at<=?))) "
                "ORDER BY priority DESC, discovered_at LIMIT ?",
                (domain, _now(), limit)).fetchall()
        return [row["url"] for row in rows]

    def claim_urls(self, domain: str, limit: int) -> list[str]:
        """Atomically claim due URLs so another worker cannot fetch them."""
        domain = bare_domain(domain)
        with _lock:
            self._maintain_frontier_locked(domain)
            self.db.execute("BEGIN IMMEDIATE")
            concurrency = max(1, min(int(os.environ.get(
                "STARTLINE_CRAWL_DOMAIN_CONCURRENCY", "2")), 20))
            in_flight = self.db.execute(
                "SELECT COUNT(*) AS n FROM url_frontier "
                "WHERE domain=? AND status='fetching'", (domain,)
            ).fetchone()["n"]
            claim_limit = min(max(0, concurrency - in_flight), max(0, limit))
            if claim_limit == 0:
                self.db.commit()
                return []
            rows = self.db.execute(
                "SELECT url FROM url_frontier WHERE domain=? AND (status='pending' OR "
                "(status='retry' AND (next_retry_at IS NULL OR next_retry_at<=?))) "
                "ORDER BY priority DESC, discovered_at LIMIT ?",
                (domain, _now(), claim_limit),
            ).fetchall()
            if rows:
                claimed_at = _now()
                self.db.executemany(
                    "UPDATE url_frontier SET status='fetching',claimed_at=? "
                    "WHERE domain=? AND url=?",
                    [(claimed_at, domain, row["url"]) for row in rows],
                )
                self.db.commit()
        return [row["url"] for row in rows]

    def frontier_counts(self, domain: str) -> dict[str, int]:
        """Count discovered URLs by state for honest crawl-exhaustion reporting."""
        domain = bare_domain(domain)
        with _lock:
            maintenance = self._maintain_frontier_locked(domain)
            rows = self.db.execute(
                "SELECT status, COUNT(*) AS n FROM url_frontier WHERE domain=? "
                "GROUP BY status", (domain,)
            ).fetchall()
            retry_due = self.db.execute(
                "SELECT COUNT(*) AS n FROM url_frontier WHERE domain=? AND status='retry' "
                "AND (next_retry_at IS NULL OR next_retry_at<=?)", (domain, _now())
            ).fetchone()["n"]
            next_retry = self.db.execute(
                "SELECT MIN(next_retry_at) AS at FROM url_frontier "
                "WHERE domain=? AND status='retry' AND next_retry_at>?",
                (domain, _now()),
            ).fetchone()["at"]
        counts = {"pending": 0, "retry": 0, "done": 0, "failed": 0,
                  "fetching": 0, "deferred_budget": 0, "total": 0}
        for row in rows:
            counts[row["status"]] = row["n"]
            counts["total"] += row["n"]
        counts["retry_due"] = retry_due
        counts["retry_waiting"] = counts["retry"] - retry_due
        counts["next_retry_at"] = next_retry
        counts["remaining"] = counts["pending"] + counts["retry"] + counts["fetching"]
        counts["budget_limited"] = counts["deferred_budget"] > 0
        counts.update(maintenance)
        return counts

    def record_url(self, domain: str, url: str, *, ok: bool, tier: str | None,
                   content_hash: str | None = None, error: str | None = None,
                   transient: bool = True) -> None:
        with _lock:
            row = self.db.execute(
                "SELECT attempts FROM url_frontier WHERE domain=? AND url=?",
                (bare_domain(domain), url),
            ).fetchone()
            attempts = (row["attempts"] if row else 0) + 1
            max_attempts = max(1, min(int(os.environ.get(
                "STARTLINE_URL_MAX_ATTEMPTS", "3")), 10))
            if ok:
                status, next_retry, terminal = "done", None, None
            elif transient and attempts < max_attempts:
                base = max(1, int(os.environ.get("STARTLINE_RETRY_BASE_SECONDS", "5")))
                delay = min(base * (2 ** (attempts - 1)), 3600)
                status = "retry"
                next_retry = (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat()
                terminal = None
            else:
                status, next_retry = "failed", None
                terminal = "attempts_exhausted" if transient else "permanent_error"
            self.db.execute(
                "UPDATE url_frontier SET status=?,last_tier=?,last_error=?,content_hash=?,"
                "last_crawled_at=?,attempts=attempts+1,next_retry_at=?,claimed_at=NULL,terminal_reason=? "
                "WHERE domain=? AND url=?",
                (status, tier, error, content_hash, _now(), next_retry, terminal,
                 bare_domain(domain), url))
            self.db.commit()

    def cancelled(self, domain: str) -> bool:
        row = self.db.execute(
            "SELECT 1 FROM jobs j JOIN companies c ON c.id=j.company_id "
            "WHERE c.domain=? AND j.source_type='official_site' "
            "AND j.status='cancelled' LIMIT 1", (bare_domain(domain),),
        ).fetchone()
        return bool(row)


# ---------------------------------------------------------------------------
# the adapter
# ---------------------------------------------------------------------------

class LadderSiteAdapter(OfficialSiteAdapter):
    """Same contract as OfficialSiteAdapter; pages come from the ladder."""

    source_type = "official_site"

    def __init__(self, store: LadderStore, max_pages: int | None = None):
        if max_pages is None:
            max_pages = max(4, min(int(os.environ.get(
                "STARTLINE_CRAWL_PAGES_PER_RUN", "40")), 1000))
        super().__init__(max_pages=max_pages)
        self.store = store

    def fetch(self, company_name: str, domain: Optional[str],
              hints=None,
              company_id: Optional[str] = None) -> list[ScrapedDoc]:
        if not domain:
            return []

        state = self.store.get(domain)
        from .discovery import discover_sitemap_urls

        wanted = state.get("want_fields") or None
        baseline_pages = min(self.max_pages, 4)
        result = ladder.crawl(
            domain,
            start_tier=state.get("floor_tier") or ladder.TIERS[0],
            # Field completion is decided only after extraction/verification.
            # This gate measures transport/content health, never fact presence.
            want_fields=None,
            max_pages=baseline_pages,
            single_tier=True,
            cancel_check=lambda: self.store.cancelled(domain),
        )
        self.store.record_run(domain, result)

        if result.base_url:
            discovered = discover_sitemap_urls(
                result.base_url, wanted,
                learned=(hints.learned if hints is not None else None),
            )
            from bs4 import BeautifulSoup
            from .discovery import DiscoveredURL, normalize_discovered_url
            from .url_safety import same_site
            for page in result.pages:
                soup = BeautifulSoup(page.html or "", "lxml")
                for link in soup.select('link[rel="canonical"][href], link[rel="alternate"][href]'):
                    candidate = normalize_discovered_url(link.get("href", ""))
                    if (candidate.startswith(("http://", "https://"))
                            and same_site(result.base_url, candidate)):
                        discovered.append(DiscoveredURL(
                            candidate, page.url, "html_link", 12))
            self.store.add_urls(domain, discovered)

        docs: list[ScrapedDoc] = []
        seen = set()
        for page in result.pages:
            if hints is not None and hints.matches_exclude(page.url):
                continue
            norm = self._norm(page.url)
            if norm in seen:
                continue
            seen.add(norm)
            docs.append(self._to_doc(page.url, page.html))

        while len(docs) < self.max_pages:
            claimed = self.store.claim_urls(domain, 1)
            if not claimed:
                break
            url = claimed[0]
            if len(docs) >= self.max_pages:
                break
            norm = self._norm(url)
            if norm in seen:
                self.store.record_url(domain, url, ok=True, tier=result.tier)
                continue
            fetched = ladder.retrieve_url(
                url, start_tier=state.get("floor_tier") or ladder.TIERS[0],
                cancel_check=lambda: self.store.cancelled(domain))
            if fetched.pages:
                page = fetched.pages[0]
                seen.add(norm)
                docs.append(self._to_doc(page.url, page.html))
                digest = hashlib.sha256(page.html.encode("utf-8", "ignore")).hexdigest()
                self.store.record_url(domain, url, ok=True, tier=fetched.tier,
                                      content_hash=digest)
            else:
                error = next((a.error or a.skipped for a in reversed(fetched.attempts)
                              if a.error or a.skipped), "no usable content")
                permanent = any(token in (error or "").lower() for token in (
                    "http 400", "http 401", "http 403", "http 404", "invalid url",
                    "unsupported scheme", "unsafe url",
                ))
                self.store.record_url(domain, url, ok=False, tier=fetched.tier,
                                      error=error, transient=not permanent)

        # operator seeds/pins are contractual: fetch them even if no tier
        # surfaced them (a plain GET is enough — they're explicit URLs)
        if hints is not None:
            robots, _delay = self._load_robots(result.base_url or f"https://{domain}/")
            for url in hints.forced_urls():
                if len(docs) >= self.max_pages:
                    break
                norm = self._norm(url)
                if norm in seen:
                    continue
                html = self._get(url, robots)
                seen.add(norm)
                if html:
                    docs.append(self._to_doc(url, html))
        # Same template removal the base adapter applies — the pages came from
        # the ladder rather than from _discover, but the chrome is identical.
        return self._strip_shared_chrome(docs)
