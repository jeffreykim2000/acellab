"""
scrape_learner.py — Local learning system for the company scraper.

Records what worked and what failed per domain so future scrapes improve
automatically.  No external API required — all data lives in data/scrape_knowledge.json.

Usage inside company_scraper.py:
    learner = ScrapeLearner()
    hints = learner.get_hints(domain)        # before scraping
    learner.record_result(domain, result)    # after scraping
    learner.record_correction(domain, field, correct_value)  # after human edit
"""
import json
import logging
import os
import re
from datetime import datetime
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

_BASE_DIR   = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DATA_DIR   = os.path.join(_BASE_DIR, "data")
_KNOWLEDGE  = os.path.join(_DATA_DIR, "scrape_knowledge.json")
_LOGS_DIR   = os.path.join(_BASE_DIR, "logs")


def _domain(url: str) -> str:
    try:
        return urlparse(url).netloc.replace("www.", "").lower()
    except Exception:
        return url


# ---------------------------------------------------------------------------
# ScrapeLearner
# ---------------------------------------------------------------------------

class ScrapeLearner:
    """
    Persistent learning store backed by a local JSON file.

    Knowledge structure per domain:
    {
      "scrape_count": 3,
      "last_scraped": "2026-05-11T10:30:00",
      "reliable_pages": ["/about", "/contact"],   # pages that consistently yield data
      "barren_pages":   ["/blog/post-1"],          # pages that never yield data
      "field_hits": {                              # how often each field was found
          "email": {"found": 5, "missing": 1},
          "industry": {"found": 4, "missing": 2}
      },
      "corrections": [                             # human corrections history
          {"field": "email", "wrong": "x@x.com", "correct": "y@y.com", "date": "..."}
      ],
      "always_missing": ["phone", "hq_location"]  # fields never found for this domain
    }
    """

    def __init__(self, knowledge_file: str = _KNOWLEDGE):
        self._path = knowledge_file
        os.makedirs(os.path.dirname(knowledge_file), exist_ok=True)
        self._data: Dict = self._load()

    # ------------------------------------------------------------------ #
    #  Persistence                                                          #
    # ------------------------------------------------------------------ #

    def _load(self) -> Dict:
        if os.path.isfile(self._path):
            try:
                with open(self._path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logger.warning("Could not load scrape knowledge: %s", e)
        return {"domains": {}, "global": {"total_scrapes": 0}}

    def _save(self) -> None:
        try:
            with open(self._path, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error("Could not save scrape knowledge: %s", e)

    def _domain_entry(self, domain: str) -> Dict:
        if domain not in self._data["domains"]:
            self._data["domains"][domain] = {
                "scrape_count": 0,
                "last_scraped": "",
                "reliable_pages": [],
                "barren_pages": [],
                "field_hits": {},
                "corrections": [],
                "always_missing": [],
            }
        return self._data["domains"][domain]

    # ------------------------------------------------------------------ #
    #  Recording results                                                   #
    # ------------------------------------------------------------------ #

    def record_result(
        self,
        url: str,
        pages_ok: List[str],
        pages_failed: List[str],
        fields_found: List[str],
        fields_missing: List[str],
    ) -> None:
        """
        Call after every scrape with what Firecrawl succeeded/failed on.
        pages_ok / pages_failed are relative paths (or full URLs, normalised internally).
        """
        domain = _domain(url)
        entry  = self._domain_entry(domain)
        now    = datetime.now().isoformat(timespec="seconds")

        entry["scrape_count"] += 1
        entry["last_scraped"] = now
        self._data["global"]["total_scrapes"] = (
            self._data["global"].get("total_scrapes", 0) + 1
        )

        # Track reliable / barren pages
        for p in pages_ok:
            path = self._to_path(p)
            if path and path not in entry["reliable_pages"]:
                entry["reliable_pages"].append(path)
            if path in entry["barren_pages"]:
                entry["barren_pages"].remove(path)

        for p in pages_failed:
            path = self._to_path(p)
            if path and path not in entry["barren_pages"] and path not in entry["reliable_pages"]:
                entry["barren_pages"].append(path)

        # Track field hit/miss rates
        for f in fields_found:
            hits = entry["field_hits"].setdefault(f, {"found": 0, "missing": 0})
            hits["found"] += 1

        for f in fields_missing:
            hits = entry["field_hits"].setdefault(f, {"found": 0, "missing": 0})
            hits["missing"] += 1

        # Update always_missing list (fields missing in every scrape for this domain)
        always = []
        for f, counts in entry["field_hits"].items():
            if counts["found"] == 0 and counts["missing"] >= 2:
                always.append(f)
        entry["always_missing"] = always

        self._save()

    def record_correction(
        self, url: str, field: str, wrong_value: Any, correct_value: Any
    ) -> None:
        """
        Call when a human corrects a scraper-produced value.
        Corrections are stored in the knowledge base so patterns can be improved.
        """
        domain = _domain(url)
        entry  = self._domain_entry(domain)
        entry["corrections"].append({
            "field":   field,
            "wrong":   str(wrong_value)[:200],
            "correct": str(correct_value)[:200],
            "date":    datetime.now().isoformat(timespec="seconds"),
        })
        # A correction on a field that was marked "always_missing" means we now
        # know the value IS findable — remove from always_missing
        if field in entry["always_missing"]:
            entry["always_missing"].remove(field)
        self._save()
        logger.info("Correction recorded for %s.%s", domain, field)

    # ------------------------------------------------------------------ #
    #  Query hints for the scraper                                         #
    # ------------------------------------------------------------------ #

    def get_hints(self, url: str) -> Dict:
        """
        Return hints the scraper should use before starting on this URL.

        Returns:
          {
            "prioritise_pages": ["/about", "/contact"],  # proven to yield data
            "skip_pages":       ["/blog/post-1"],         # known to be barren
            "always_missing":   ["phone"],                # don't waste time on these
            "corrections":      [...]                     # latest human corrections
          }
        """
        domain = _domain(url)
        entry  = self._data["domains"].get(domain, {})
        return {
            "prioritise_pages": entry.get("reliable_pages", [])[:10],
            "skip_pages":       entry.get("barren_pages", [])[:20],
            "always_missing":   entry.get("always_missing", []),
            "corrections":      entry.get("corrections", [])[-5:],
            "scrape_count":     entry.get("scrape_count", 0),
        }

    def domain_summary(self, url: str) -> Dict:
        """Return full knowledge entry for a domain (for the log file)."""
        return self._data["domains"].get(_domain(url), {})

    # ------------------------------------------------------------------ #
    #  Helpers                                                             #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _to_path(url_or_path: str) -> str:
        """Normalise a full URL to just its path component."""
        try:
            p = urlparse(url_or_path).path.rstrip("/") or "/"
            return p
        except Exception:
            return url_or_path


# ---------------------------------------------------------------------------
# Scrape log writer
# ---------------------------------------------------------------------------

def write_scrape_log(
    company_name: str,
    url: str,
    source_results: Dict[str, Dict],
    source_errors: Dict[str, str],
    page_results: Dict,
    missing_critical: List[str],
    overall_confidence: float,
    conflicts: List[Dict],
) -> str:
    """
    Write a structured JSON log for one scrape run.
    Returns the path of the written file.

    Fields captured:
    - Which sources ran and how many fields each returned
    - Per-page scrape success/failure
    - Which critical fields are missing and why
    - Merge conflicts between sources
    """
    os.makedirs(_LOGS_DIR, exist_ok=True)
    slug  = re.sub(r"[^\w]", "_", company_name.lower())[:30]
    ts    = datetime.now().strftime("%Y%m%d_%H%M%S")
    fname = f"scrape_{ts}_{slug}.json"
    fpath = os.path.join(_LOGS_DIR, fname)

    # Build per-source summary
    source_summary = {}
    for src, data in source_results.items():
        non_empty = {k: v for k, v in data.items()
                     if not k.startswith("_") and v not in (None, "", [], {})}
        source_summary[src] = {
            "fields_found":   sorted(non_empty.keys()),
            "field_count":    len(non_empty),
            "error":          source_errors.get(src),
        }

    # Missing field diagnostics — which source might have had it
    missing_diagnosis = {}
    for field in missing_critical:
        candidates = []
        for src, data in source_results.items():
            val = data.get(field)
            if val not in (None, "", [], {}):
                candidates.append({"source": src, "value": str(val)[:120]})
        missing_diagnosis[field] = {
            "found_in_any_source": bool(candidates),
            "candidates":          candidates,
            "note": (
                "Found in a source but lost during merge — check source weights"
                if candidates else
                "Not found in any source — website may not expose this field"
            ),
        }

    report = {
        "timestamp":          datetime.now().isoformat(timespec="seconds"),
        "company_name":       company_name,
        "url":                url,
        "overall_confidence": f"{round(overall_confidence * 100)}%",
        "missing_critical":   missing_critical,
        "missing_diagnosis":  missing_diagnosis,
        "sources":            source_summary,
        "pages": {
            "attempted":      page_results.get("attempted", 0),
            "succeeded":      page_results.get("succeeded", 0),
            "sitemap_urls":   page_results.get("sitemap_urls", 0),
            "map_urls":       page_results.get("map_urls", 0),
            "failures":       page_results.get("failures", []),
        },
        "merge_conflicts":    conflicts,
    }

    try:
        with open(fpath, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        logger.info("Scrape log written: %s", fpath)
    except Exception as e:
        logger.error("Could not write scrape log: %s", e)

    return fpath
