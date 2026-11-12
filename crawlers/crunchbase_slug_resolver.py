"""
CrunchbaseSlugResolver — verifies the correct Crunchbase organization slug
before any Crunchbase scrape, preventing silent data corruption from
wrong-company matches (Notion IoT vs Notion.so, Scale AI Canada vs Scale AI US, etc.)
"""
import json
import logging
import os
import re
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import httpx

logger = logging.getLogger(__name__)

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_AUTOCOMPLETE_URL = (
    "https://autocomplete.crunchbase.com/v1/autocomplete"
    "?q={query}&limit=10&collection_ids=organizations"
)

_COMMON_SUFFIXES = [
    "incorporated", "inc", "limited", "ltd", "corporation", "corp",
    "llc", "co", "ai", "technologies", "technology", "solutions",
    "labs", "research", "group", "systems", "services", "software",
    "platform", "platforms",
]

_STOPWORDS = frozenset([
    "the", "a", "an", "of", "in", "is", "and", "or", "to", "for",
    "that", "with", "on", "at", "by", "from", "as", "it", "its",
    "be", "are", "was", "were",
])


# ---------------------------------------------------------------------------
# Data container
# ---------------------------------------------------------------------------

@dataclass
class SlugResolution:
    slug: Optional[str]
    crunchbase_url: Optional[str]
    confidence: float            # 0.0 – 1.0
    resolution_method: str       # "cached" | "search_match" | "direct_verified" | "fuzzy_match" | "manual_required" | "not_found"
    candidates_evaluated: list   # list of dicts for audit
    reasoning: str


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def _jaro_winkler(s1: str, s2: str) -> float:
    """
    Jaro-Winkler similarity between two strings.
    Both are lowercased before comparison.
    Returns a float in [0.0, 1.0].
    """
    s1 = s1.lower().strip()
    s2 = s2.lower().strip()

    if not s1 or not s2:
        return 0.0
    if s1 == s2:
        return 1.0

    len1, len2 = len(s1), len(s2)
    match_dist = max(len1, len2) // 2 - 1
    if match_dist < 0:
        match_dist = 0

    s1_matches = [False] * len1
    s2_matches = [False] * len2
    matches = 0
    transpositions = 0

    for i in range(len1):
        start = max(0, i - match_dist)
        end = min(i + match_dist + 1, len2)
        for j in range(start, end):
            if s2_matches[j] or s1[i] != s2[j]:
                continue
            s1_matches[i] = True
            s2_matches[j] = True
            matches += 1
            break

    if matches == 0:
        return 0.0

    # Count transpositions
    k = 0
    for i in range(len1):
        if not s1_matches[i]:
            continue
        while not s2_matches[k]:
            k += 1
        if s1[i] != s2[k]:
            transpositions += 1
        k += 1

    jaro = (
        matches / len1
        + matches / len2
        + (matches - transpositions / 2) / matches
    ) / 3.0

    # Winkler prefix bonus (up to 4 chars)
    prefix = 0
    for i in range(min(4, len1, len2)):
        if s1[i] == s2[i]:
            prefix += 1
        else:
            break

    return jaro + prefix * 0.1 * (1.0 - jaro)


def _norm_domain(url: str) -> str:
    """
    Normalize a URL to just its bare domain (no scheme, no www., no path/query).
    "https://www.notion.so/about?x=1" -> "notion.so"
    """
    if not url:
        return ""
    url = url.strip().lower()
    # Strip scheme
    url = re.sub(r'^https?://', '', url)
    # Strip www.
    url = re.sub(r'^www\.', '', url)
    # Strip path, query, fragment
    url = re.split(r'[/?#]', url)[0]
    # Strip port
    url = url.split(':')[0]
    return url.strip()


def _derive_slug_candidates(name: str) -> List[str]:
    """
    Derive candidate Crunchbase org slugs from a company name.
    Returns list from most-specific to least-specific.
    """
    # Baseline: lowercase, replace non-alnum runs with single hyphen, strip edges
    base = re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')
    candidates = [base]

    # Try removing trailing suffix tokens one at a time
    tokens = re.split(r'[-]+', base)
    for i in range(len(tokens) - 1, 0, -1):
        suffix = tokens[i]
        if suffix in _COMMON_SUFFIXES:
            trimmed = '-'.join(tokens[:i]).strip('-')
            if trimmed and trimmed not in candidates:
                candidates.append(trimmed)
        else:
            break  # stop at first non-suffix

    # Deduplicate preserving order, return at most 4
    seen = []
    for c in candidates:
        if c and c not in seen:
            seen.append(c)
    return seen[:4]


def _extract_founded_year(value: Any) -> Optional[int]:
    """
    Extract a 4-digit founded year from varied representations:
    - int: 2009
    - str: "2009-01-01" or "2009"
    - dict: {"value": "2009-01-01"} or {"value": 2009}
    """
    if value is None:
        return None
    if isinstance(value, int):
        return value if 1800 < value < 2100 else None
    if isinstance(value, dict):
        value = value.get("value")
        return _extract_founded_year(value)
    if isinstance(value, str):
        value = value.strip()
        # Match 4-digit year anywhere in the string
        m = re.search(r'\b(1[89]\d\d|20\d\d)\b', value)
        if m:
            return int(m.group(1))
    return None


def _meaningful_words(text: str) -> set:
    """Return non-stopword alphabetic tokens from text."""
    tokens = re.findall(r'[a-z]+', (text or "").lower())
    return {t for t in tokens if t not in _STOPWORDS and len(t) > 2}


# ---------------------------------------------------------------------------
# Main resolver class
# ---------------------------------------------------------------------------

class CrunchbaseSlugResolver:
    """
    Resolves and caches the correct Crunchbase organization slug for a company,
    preventing silent data corruption from wrong-company matches.
    """

    def __init__(self, apify_client=None, db_path: str = None):
        self._apify = apify_client

        if db_path is None:
            db_dir = os.path.join(_PROJECT_ROOT, "data")
            os.makedirs(db_dir, exist_ok=True)
            db_path = os.path.join(db_dir, "slug_cache.db")
        else:
            db_dir = os.path.dirname(db_path)
            if db_dir:
                os.makedirs(db_dir, exist_ok=True)

        self._db_path = db_path
        self._init_db()

    # ------------------------------------------------------------------ #
    #  DB management                                                       #
    # ------------------------------------------------------------------ #

    def _init_db(self) -> None:
        """Create SQLite schema if it doesn't exist."""
        try:
            with sqlite3.connect(self._db_path) as conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS crunchbase_slug_resolutions (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        entity_id TEXT,
                        company_name_input TEXT NOT NULL,
                        known_signals TEXT,
                        resolved_slug TEXT,
                        resolved_url TEXT,
                        confidence REAL,
                        resolution_method TEXT,
                        candidates_evaluated TEXT,
                        resolved_at TEXT,
                        resolved_by TEXT DEFAULT 'auto'
                    )
                """)
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_slug_res_name_slug
                    ON crunchbase_slug_resolutions (company_name_input, resolved_slug)
                """)
                conn.commit()
        except sqlite3.OperationalError as e:
            logger.error("Failed to initialise slug cache DB at %s: %s", self._db_path, e)

    def _db_lookup(self, company_name: str) -> Optional[dict]:
        """Return the most recent cached row for this company name, or None."""
        try:
            with sqlite3.connect(self._db_path) as conn:
                conn.row_factory = sqlite3.Row
                row = conn.execute(
                    """
                    SELECT * FROM crunchbase_slug_resolutions
                    WHERE LOWER(company_name_input) = LOWER(?)
                    ORDER BY id DESC LIMIT 1
                    """,
                    (company_name,),
                ).fetchone()
                if row:
                    return dict(row)
        except sqlite3.OperationalError as e:
            logger.warning("DB lookup failed: %s", e)
        return None

    def _db_upsert(
        self,
        company_name: str,
        slug: Optional[str],
        resolved_url: Optional[str],
        confidence: float,
        resolution_method: str,
        candidates_evaluated: list,
        known_signals: Optional[dict] = None,
        resolved_by: str = "auto",
        entity_id: Optional[str] = None,
    ) -> None:
        """Insert a new resolution row (we keep history, so always INSERT)."""
        now = datetime.now(timezone.utc).isoformat()
        try:
            with sqlite3.connect(self._db_path) as conn:
                conn.execute(
                    """
                    INSERT INTO crunchbase_slug_resolutions
                        (entity_id, company_name_input, known_signals, resolved_slug,
                         resolved_url, confidence, resolution_method, candidates_evaluated,
                         resolved_at, resolved_by)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        entity_id,
                        company_name,
                        json.dumps(known_signals or {}),
                        slug,
                        resolved_url,
                        confidence,
                        resolution_method,
                        json.dumps(candidates_evaluated),
                        now,
                        resolved_by,
                    ),
                )
                conn.commit()
        except sqlite3.OperationalError as e:
            logger.warning("DB upsert failed: %s", e)

    # ------------------------------------------------------------------ #
    #  Public API                                                          #
    # ------------------------------------------------------------------ #

    def _strategy_sitemap(self, company_name: str) -> Optional[SlugResolution]:
        """Exact match against Crunchbase's own published sitemap index.

        Returns None when the index has never been built or does not contain
        the company, so the caller falls through to the paid strategies. A
        miss is genuinely inconclusive: the index is a subset of Crunchbase,
        not a census of it.
        """
        try:
            from .crunchbase_sitemap import CrunchbaseSitemapIndex
            index = CrunchbaseSitemapIndex(db_path=self._db_path)
            if not index.covers():
                return None
            hit = index.lookup(company_name)
        except Exception as exc:              # noqa: BLE001 — never block resolution
            logger.warning("sitemap index lookup failed: %s", exc)
            return None
        if not hit:
            return None
        return SlugResolution(
            slug=hit["slug"],
            crunchbase_url=hit["crunchbase_url"],
            confidence=0.75,
            resolution_method="sitemap_index",
            candidates_evaluated=[],
            reasoning=("Exact normalized-name match in Crunchbase's published "
                       f"sitemap index (people page: {bool(hit['people_url'])}, "
                       f"funding page: {bool(hit['funding_url'])})"),
        )

    def resolve_slug(self, company_name: str, known_signals: dict) -> SlugResolution:
        """
        Resolve the correct Crunchbase slug for `company_name`.
        Uses a 4-strategy waterfall (cache → autocomplete → direct → manual).
        """
        candidates_evaluated: List[dict] = []

        # ── Strategy A: SQLite cache ──────────────────────────────────────
        cached = self._db_lookup(company_name)
        if (
            cached
            and cached.get("resolution_method") != "manual_required"
            and (cached.get("confidence") or 0.0) >= 0.7
        ):
            slug = cached.get("resolved_slug")
            cb_url = cached.get("resolved_url") or (
                f"https://www.crunchbase.com/organization/{slug}" if slug else None
            )
            result = SlugResolution(
                slug=slug,
                crunchbase_url=cb_url,
                confidence=1.0,
                resolution_method="cached",
                candidates_evaluated=[],
                reasoning=f"Returned from cache (original method: {cached.get('resolution_method')}, "
                          f"confidence: {cached.get('confidence', 0):.2f})",
            )
            logger.info(
                "slug_resolved",
                extra={
                    "action": "slug_resolved",
                    "company_name": company_name,
                    "resolution_method": result.resolution_method,
                    "confidence": result.confidence,
                    "slug": result.slug,
                    "candidates_count": len(result.candidates_evaluated),
                },
            )
            return result

        # ── Strategy A2: local sitemap index ──────────────────────────────
        # Crunchbase publishes its organization URLs as plain-text sitemaps,
        # so an exact name match against that index is free, offline and
        # instant. Tried before every network strategy below, which all cost
        # either an API call or Apify credits.
        #
        # Confidence is deliberately just over the caller's 0.7 bar rather
        # than 1.0: an exact normalized-name match is strong evidence but not
        # proof of identity, and CrunchbaseAdapter still cross-checks the
        # fetched record's website against the company's own domain before
        # asserting anything.
        sitemap_hit = self._strategy_sitemap(company_name)
        if sitemap_hit is not None:
            self._db_upsert(
                company_name=company_name,
                slug=sitemap_hit.slug,
                resolved_url=sitemap_hit.crunchbase_url,
                confidence=sitemap_hit.confidence,
                resolution_method=sitemap_hit.resolution_method,
                candidates_evaluated=sitemap_hit.candidates_evaluated,
                known_signals=known_signals,
            )
            logger.info("slug_resolved", extra={
                "action": "slug_resolved", "company_name": company_name,
                "resolution_method": sitemap_hit.resolution_method,
                "confidence": sitemap_hit.confidence, "slug": sitemap_hit.slug,
                "candidates_count": 0})
            return sitemap_hit

        # ── Strategy B: Crunchbase autocomplete API ───────────────────────
        strategy_b_result = self._strategy_b(company_name, known_signals, candidates_evaluated)
        if strategy_b_result is not None and strategy_b_result.confidence >= 0.7:
            self._db_upsert(
                company_name=company_name,
                slug=strategy_b_result.slug,
                resolved_url=strategy_b_result.crunchbase_url,
                confidence=strategy_b_result.confidence,
                resolution_method=strategy_b_result.resolution_method,
                candidates_evaluated=strategy_b_result.candidates_evaluated,
                known_signals=known_signals,
            )
            logger.info(
                "slug_resolved",
                extra={
                    "action": "slug_resolved",
                    "company_name": company_name,
                    "resolution_method": strategy_b_result.resolution_method,
                    "confidence": strategy_b_result.confidence,
                    "slug": strategy_b_result.slug,
                    "candidates_count": len(strategy_b_result.candidates_evaluated),
                },
            )
            return strategy_b_result

        # ── Strategy C: Direct slug derivation + Apify verification ──────
        strategy_c_result = self._strategy_c(company_name, known_signals, candidates_evaluated)
        if strategy_c_result is not None and strategy_c_result.confidence >= 0.7:
            self._db_upsert(
                company_name=company_name,
                slug=strategy_c_result.slug,
                resolved_url=strategy_c_result.crunchbase_url,
                confidence=strategy_c_result.confidence,
                resolution_method=strategy_c_result.resolution_method,
                candidates_evaluated=strategy_c_result.candidates_evaluated,
                known_signals=known_signals,
            )
            logger.info(
                "slug_resolved",
                extra={
                    "action": "slug_resolved",
                    "company_name": company_name,
                    "resolution_method": strategy_c_result.resolution_method,
                    "confidence": strategy_c_result.confidence,
                    "slug": strategy_c_result.slug,
                    "candidates_count": len(strategy_c_result.candidates_evaluated),
                },
            )
            return strategy_c_result

        # ── Strategy D: Manual required ───────────────────────────────────
        result = SlugResolution(
            slug=None,
            crunchbase_url=None,
            confidence=0.0,
            resolution_method="manual_required",
            candidates_evaluated=candidates_evaluated,
            reasoning=(
                f"All automated strategies failed for {company_name!r}. "
                f"Evaluated {len(candidates_evaluated)} candidate(s). "
                "Manual slug confirmation required."
            ),
        )
        self._db_upsert(
            company_name=company_name,
            slug=None,
            resolved_url=None,
            confidence=0.0,
            resolution_method="manual_required",
            candidates_evaluated=candidates_evaluated,
            known_signals=known_signals,
        )
        logger.warning(
            "Crunchbase slug manual review required for %r — %d candidate(s) evaluated",
            company_name,
            len(candidates_evaluated),
        )
        logger.info(
            "slug_resolved",
            extra={
                "action": "slug_resolved",
                "company_name": company_name,
                "resolution_method": result.resolution_method,
                "confidence": result.confidence,
                "slug": result.slug,
                "candidates_count": len(result.candidates_evaluated),
            },
        )
        return result

    def verify_slug_matches_entity(
        self, slug: str, known_signals: dict
    ) -> Tuple[bool, dict]:
        """
        Use the pratikdani Apify actor to scrape the Crunchbase page for `slug`
        and compare the scraped entity attributes to `known_signals`.

        Returns (is_match, comparison_details_dict).
        """
        if not self._apify:
            return False, {"error": "No apify client configured"}

        cb_url = f"https://www.crunchbase.com/organization/{slug}"
        try:
            items = self._apify.run_actor(
                "pratikdani~crunchbase-companies-bulk-scraper-no-cookies",
                {"urls": [cb_url]},
            )
        except Exception as e:
            logger.warning("Apify verification failed for slug %r: %s", slug, e)
            return False, {"error": str(e)}

        if not items or not isinstance(items[0], dict):
            return False, {"error": "Empty or invalid Apify response"}

        d = items[0]
        props = d.get("properties") or {}

        # Extract scraped fields
        scraped_name: Optional[str] = (props.get("identifier") or {}).get("value")
        scraped_website: Optional[str] = props.get("homepage_url")
        scraped_year: Optional[int] = _extract_founded_year(props.get("founded_on"))

        comparison: dict = {
            "scraped_name": scraped_name,
            "scraped_website": scraped_website,
            "scraped_year": scraped_year,
        }

        # Website comparison (definitive if both sides have a domain)
        sig_website = known_signals.get("website_url", "")
        sig_domain = _norm_domain(sig_website)
        scraped_domain = _norm_domain(scraped_website or "")

        if sig_domain and scraped_domain:
            website_match = sig_domain == scraped_domain
            comparison["website_match"] = website_match
            comparison["sig_domain"] = sig_domain
            comparison["scraped_domain"] = scraped_domain
            return website_match, comparison

        # Name comparison
        sig_name = known_signals.get("company_name", "")
        name_match = False
        if scraped_name and sig_name:
            jw = _jaro_winkler(scraped_name, sig_name)
            comparison["name_jw"] = jw
            name_match = jw > 0.85

        # Year comparison
        sig_year = known_signals.get("founded_year")
        year_match = False
        if scraped_year and sig_year:
            try:
                year_match = abs(int(scraped_year) - int(sig_year)) <= 1
            except (TypeError, ValueError):
                pass
            comparison["year_match"] = year_match

        # Decide
        if name_match and sig_year and year_match:
            comparison["decision"] = "name+year match"
            return True, comparison
        if name_match and not sig_year:
            comparison["decision"] = "name match (no year signal)"
            return True, comparison
        if not sig_domain and not sig_name:
            comparison["decision"] = "no signals to compare"
            return False, comparison

        comparison["decision"] = "no definitive match"
        return False, comparison

    def confirm_slug(
        self, company_name_input: str, slug: str, resolved_by: str = "manual"
    ) -> SlugResolution:
        """Manually confirm a slug. Stored with confidence=1.0, method='cached'."""
        cb_url = f"https://www.crunchbase.com/organization/{slug}"
        self._db_upsert(
            company_name=company_name_input,
            slug=slug,
            resolved_url=cb_url,
            confidence=1.0,
            resolution_method="cached",
            candidates_evaluated=[],
            resolved_by=resolved_by,
        )
        return SlugResolution(
            slug=slug,
            crunchbase_url=cb_url,
            confidence=1.0,
            resolution_method="cached",
            candidates_evaluated=[],
            reasoning=f"Manually confirmed by {resolved_by}",
        )

    def mark_not_on_crunchbase(self, company_name_input: str) -> None:
        """Mark a company as definitively not on Crunchbase."""
        self._db_upsert(
            company_name=company_name_input,
            slug=None,
            resolved_url=None,
            confidence=0.9,
            resolution_method="not_found",
            candidates_evaluated=[],
            resolved_by="manual",
        )

    def get_pending_reviews(self) -> List[dict]:
        """Return all rows where manual resolution is needed, newest first."""
        try:
            with sqlite3.connect(self._db_path) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    """
                    SELECT * FROM crunchbase_slug_resolutions
                    WHERE resolution_method = 'manual_required'
                    ORDER BY resolved_at DESC
                    """
                ).fetchall()
                result = []
                for row in rows:
                    d = dict(row)
                    for key in ("known_signals", "candidates_evaluated"):
                        try:
                            d[key] = json.loads(d.get(key) or "[]")
                        except (json.JSONDecodeError, TypeError):
                            d[key] = []
                    result.append(d)
                return result
        except sqlite3.OperationalError as e:
            logger.warning("get_pending_reviews failed: %s", e)
            return []

    def get_all_resolutions(self, limit: int = 100) -> List[dict]:
        """Return the most recent `limit` resolution rows, newest first."""
        try:
            with sqlite3.connect(self._db_path) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    """
                    SELECT * FROM crunchbase_slug_resolutions
                    ORDER BY id DESC LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
                result = []
                for row in rows:
                    d = dict(row)
                    for key in ("known_signals", "candidates_evaluated"):
                        try:
                            d[key] = json.loads(d.get(key) or "[]")
                        except (json.JSONDecodeError, TypeError):
                            d[key] = []
                    result.append(d)
                return result
        except sqlite3.OperationalError as e:
            logger.warning("get_all_resolutions failed: %s", e)
            return []

    # ------------------------------------------------------------------ #
    #  Internal strategy implementations                                   #
    # ------------------------------------------------------------------ #

    def _strategy_b(
        self,
        company_name: str,
        known_signals: dict,
        candidates_evaluated: List[dict],
    ) -> Optional[SlugResolution]:
        """
        Strategy B: Query Crunchbase public autocomplete API, score results.
        Returns a SlugResolution if confident enough, else None.
        """
        try:
            time.sleep(1.0)  # polite rate-limit delay
            url = _AUTOCOMPLETE_URL.format(query=httpx.URL(company_name).raw_path.decode() if False else company_name)
            # Build URL manually to avoid encoding issues
            encoded_name = httpx.URL("", params={"q": company_name}).params.get("q", company_name)
            url = (
                f"https://autocomplete.crunchbase.com/v1/autocomplete"
                f"?q={encoded_name}&limit=10&collection_ids=organizations"
            )
            with httpx.Client(timeout=15.0) as client:
                resp = client.get(
                    "https://autocomplete.crunchbase.com/v1/autocomplete",
                    params={
                        "q": company_name,
                        "limit": 10,
                        "collection_ids": "organizations",
                    },
                    headers={"User-Agent": "Mozilla/5.0"},
                )
            if resp.status_code != 200:
                logger.warning(
                    "Crunchbase autocomplete returned %d for %r", resp.status_code, company_name
                )
                return None
            data = resp.json()
        except Exception as e:
            logger.warning("Crunchbase autocomplete request failed for %r: %s", company_name, e)
            return None

        entities = data.get("entities") or []
        if not entities:
            return None

        scored: List[Tuple[float, dict]] = []
        for ent in entities:
            ident = ent.get("identifier") or {}
            slug = ident.get("permalink") or ""
            name = ident.get("value") or ""
            short_desc = ent.get("short_description") or ""
            location_ids = ent.get("location_identifiers") or []
            rank_org = ent.get("rank_org") or 9999

            # Extract country from location_identifiers
            country = ""
            for loc in location_ids:
                if isinstance(loc, dict) and loc.get("location_type") == "country":
                    country = (loc.get("value") or "").lower()
                    break

            score = 0.0
            jw = _jaro_winkler(name, company_name)
            if jw > 0.95:
                score += 0.50
            elif jw >= 0.80:
                score += 0.30

            # Country signal
            sig_country = (known_signals.get("hq_country") or "").lower()
            if sig_country and country and sig_country in country:
                score += 0.15

            # Description overlap
            sig_desc = known_signals.get("description") or ""
            if sig_desc:
                sig_words = _meaningful_words(sig_desc)
                cand_words = _meaningful_words(short_desc)
                shared = sig_words & cand_words
                if len(shared) >= 3:
                    score += 0.10

            # Rank bonus for well-known companies
            if rank_org and rank_org < 100:
                score += 0.05

            candidate_record = {
                "slug": slug,
                "name": name,
                "short_description": short_desc,
                "country": country,
                "rank_org": rank_org,
                "jw_score": round(jw, 4),
                "total_score": round(score, 4),
                "source": "autocomplete",
            }
            scored.append((score, candidate_record))
            candidates_evaluated.append(candidate_record)

        if not scored:
            return None

        scored.sort(key=lambda x: x[0], reverse=True)
        top_score, top_candidate = scored[0]

        if top_score <= 0.35:
            return None

        website_signal = known_signals.get("website_url", "")

        # Try verification with apify if we have a website signal
        if website_signal and self._apify:
            # Try top 3 candidates
            for score, cand in scored[:3]:
                if score <= 0.35:
                    break
                slug = cand["slug"]
                if not slug:
                    continue
                verify_signals = dict(known_signals)
                verify_signals["company_name"] = company_name
                is_match, details = self.verify_slug_matches_entity(slug, verify_signals)
                cand["verify_details"] = details
                cand["verified_match"] = is_match
                if is_match:
                    cb_url = f"https://www.crunchbase.com/organization/{slug}"
                    return SlugResolution(
                        slug=slug,
                        crunchbase_url=cb_url,
                        confidence=0.95,
                        resolution_method="search_match",
                        candidates_evaluated=candidates_evaluated,
                        reasoning=(
                            f"Autocomplete top candidate {slug!r} (score={score:.2f}, "
                            f"JW={cand['jw_score']:.2f}) verified via website domain match."
                        ),
                    )
            # None of the top 3 verified
            return None

        if not website_signal or not self._apify:
            # Return best candidate with reduced confidence
            slug = top_candidate["slug"]
            if not slug:
                return None
            cb_url = f"https://www.crunchbase.com/organization/{slug}"

            if website_signal:
                # Has website but no apify: moderate confidence
                confidence = min(0.60 + top_score * 0.40, 0.88)
                method = "search_match"
                reasoning = (
                    f"Autocomplete match {slug!r} (score={top_score:.2f}) — "
                    "website signal present but Apify unavailable for verification."
                )
            else:
                # No website signal: lower confidence
                confidence = min(0.55 + top_score * 0.35, 0.85)
                method = "fuzzy_match"
                reasoning = (
                    f"Autocomplete best match {slug!r} (score={top_score:.2f}) — "
                    "no website signal available for verification."
                )

            if confidence < 0.7:
                return None

            return SlugResolution(
                slug=slug,
                crunchbase_url=cb_url,
                confidence=confidence,
                resolution_method=method,
                candidates_evaluated=candidates_evaluated,
                reasoning=reasoning,
            )

        return None

    def _strategy_c(
        self,
        company_name: str,
        known_signals: dict,
        candidates_evaluated: List[dict],
    ) -> Optional[SlugResolution]:
        """
        Strategy C: Derive slug candidates directly from the company name, verify each.
        Returns a SlugResolution if any candidate verifies, else None.
        """
        slug_candidates = _derive_slug_candidates(company_name)

        for slug in slug_candidates[:4]:
            candidate_record = {
                "slug": slug,
                "source": "derived",
                "verified_match": False,
                "verify_details": {},
            }

            if self._apify:
                verify_signals = dict(known_signals)
                verify_signals["company_name"] = company_name
                is_match, details = self.verify_slug_matches_entity(slug, verify_signals)
                candidate_record["verify_details"] = details
                candidate_record["verified_match"] = is_match

                if is_match:
                    website_signal = known_signals.get("website_url", "")
                    # Determine confidence based on what matched
                    if website_signal and details.get("website_match"):
                        confidence = 0.82
                        reasoning = (
                            f"Derived slug {slug!r} verified via website domain match."
                        )
                    else:
                        confidence = 0.72
                        reasoning = (
                            f"Derived slug {slug!r} verified via name/year match "
                            "(no website signal)."
                        )

                    candidates_evaluated.append(candidate_record)
                    cb_url = f"https://www.crunchbase.com/organization/{slug}"
                    return SlugResolution(
                        slug=slug,
                        crunchbase_url=cb_url,
                        confidence=confidence,
                        resolution_method="direct_verified",
                        candidates_evaluated=candidates_evaluated,
                        reasoning=reasoning,
                    )

            candidates_evaluated.append(candidate_record)

        return None
