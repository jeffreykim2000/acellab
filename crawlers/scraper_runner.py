"""
scraper_runner.py — Company data scraper for admin console.

Priority order per URL:
  1. CompanyDataScraper — full multi-API pipeline (HasData + Firecrawl + Apify)
     Uses: industry, size, founded year, HQ, funding, team, products, pricing
  2. scraper_core       — basic website-only fallback (no API keys needed)

The Selenium preview (live screenshots) runs as a visual layer on top of the
API pipeline when use_selenium=True. Data always comes from the APIs.
"""
import json
import logging
import os
import re
from typing import Callable, Dict, List, Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# ── Rich multi-API pipeline ────────────────────────────────────────────
_HAS_RICH = False

# ── Fallback: basic single-page website scraper ───────────────────────
from .scraper_core import (
    scrape_page_static,
    scrape_page_selenium,
    get_selenium_driver,
    close_popups_selenium,
)


# ───────────────────────────────────────────────────────────────────────
#  Helpers
# ───────────────────────────────────────────────────────────────────────

def _domain_to_name(url: str) -> str:
    """
    Extract a readable company name from a URL.
    endorobo.com → "Endorobo"   |   stripe.com → "Stripe"
    """
    try:
        netloc = urlparse(url).netloc.replace("www.", "")
        raw = netloc.split(".")[0]
        return re.sub(r"[-_]", " ", raw).title()
    except Exception:
        return ""


def _profile_to_row(profile, source_url: str) -> Dict:
    """
    Flatten a CompanyProfile into a flat dict suitable for JSON / Excel export.
    All list fields are JSON-encoded strings so they survive Excel serialisation.
    """
    d = profile.to_dict()
    v = d.get("_validation") or {}
    fields_meta = v.get("fields") or {}

    def _conf(key: str) -> str:
        return (fields_meta.get(key) or {}).get("confidence", "")

    def _jdump(val) -> str:
        if not val:
            return ""
        return json.dumps(val, ensure_ascii=False)

    # Flatten team: show first 5 names in a readable string
    team = d.get("team_members") or []
    team_summary = "; ".join(
        f"{m.get('name', '')} ({m.get('role', '')})"
        for m in team[:5]
        if m.get("name")
    )

    # Flatten funding rounds into a readable summary
    rounds = d.get("funding_rounds") or []
    funding_summary = "; ".join(
        f"{r.get('round_type', '')} ${r.get('amount_usd', 'N/A'):,} {r.get('announced_date', '')}"
        if isinstance(r.get("amount_usd"), int)
        else f"{r.get('round_type', '')} undisclosed {r.get('announced_date', '')}"
        for r in rounds[:5]
    )

    return {
        # ── Identity ───────────────────────────────────────────────────
        "source_url":         source_url,
        "company_name":       d.get("name", ""),
        "website":            d.get("url", source_url),
        "introduction":       d.get("introduction", ""),
        "industry":           d.get("industry", ""),
        "company_size":       d.get("size", ""),
        "founded_year":       str(d.get("founded_year") or ""),
        "hq_location":        d.get("hq_location", ""),
        "address":            d.get("address", ""),
        "tags":               ", ".join(d.get("tags") or []),
        "linkedin_url":       d.get("linkedin_url", ""),
        # ── Contact ────────────────────────────────────────────────────
        "contact_email":      d.get("email", ""),
        "contact_phone":      d.get("phone", ""),
        # ── Products & pricing ─────────────────────────────────────────
        "pricing_model":      d.get("pricing_model", ""),
        "revenue_range":      d.get("revenue_range", ""),  # often blank for private cos
        "gross_margin":       d.get("gross_margin", ""),   # rarely public — manual entry recommended
        "products_json":      _jdump(d.get("products")),
        # ── Team ───────────────────────────────────────────────────────
        "team_summary":       team_summary,
        "team_json":          _jdump(team),
        # ── Funding ────────────────────────────────────────────────────
        "funding_summary":    funding_summary,
        "funding_rounds_json": _jdump(rounds),
        "total_funding_usd":  str(d.get("total_funding") or ""),
        "investors":          ", ".join(d.get("investors") or []),
        # ── Accuracy metadata ──────────────────────────────────────────
        "overall_confidence": str(round((v.get("overall_confidence") or 0) * 100)) + "%",
        "missing_fields":     ", ".join(v.get("missing_critical") or []),
        "conflict_count":     str(len(v.get("conflicts") or [])),
        "data_sources":       ", ".join(
            sorted(set((d.get("_source_map") or {}).values()) - {"merged", "unknown"})
        ),
    }


def _fallback_to_row(core_row: Dict) -> Dict:
    """
    Convert a scraper_core row (basic website scrape) to the unified row format
    so both pipelines produce the same column schema.
    """
    return {
        "source_url":          core_row.get("source_url", ""),
        "company_name":        core_row.get("company_name", ""),
        "website":             core_row.get("website", ""),
        "introduction":        core_row.get("description", ""),
        "industry":            "",
        "company_size":        "",
        "founded_year":        "",
        "hq_location":         "",
        "address":             core_row.get("contact_address", ""),
        "tags":                "",
        "linkedin_url":        "",
        "contact_email":       core_row.get("contact_emails", ""),
        "contact_phone":       core_row.get("contact_phones", ""),
        "pricing_model":       "",
        "revenue_range":       "",
        "gross_margin":        "",
        "products_json":       core_row.get("products_json", ""),
        "team_summary":        core_row.get("founders_json", ""),
        "team_json":           core_row.get("employees_json", ""),
        "funding_summary":     core_row.get("funding_snippets", ""),
        "funding_rounds_json": "",
        "total_funding_usd":   "",
        "investors":           core_row.get("investors", ""),
        "overall_confidence":  "low (website only)",
        "missing_fields":      "industry, size, founded_year, hq_location, team, funding",
        "conflict_count":      "0",
        "data_sources":        "website",
    }


# ───────────────────────────────────────────────────────────────────────
#  Rich scrape: one URL through the full API pipeline
# ───────────────────────────────────────────────────────────────────────

def _scrape_rich(url: str, log: Callable) -> Optional[Dict]:
    """
    Run the full CompanyDataScraper pipeline for one URL.
    Returns a flat row dict on success, None on failure.
    """
    if not _HAS_RICH:
        return None

    hasdata_key   = os.environ.get("HASDATA_API_KEY", "")
    firecrawl_key = os.environ.get("FIRECRAWL_API_KEY", "")
    apify_token   = os.environ.get("APIFY_API_TOKEN", "")

    # Warn if only Firecrawl is configured
    if not hasdata_key:
        log("  [info] HASDATA_API_KEY not set — skipping SERP/Maps/LinkedIn enrichment")
    if not apify_token:
        log("  [info] APIFY_API_TOKEN not set — skipping LinkedIn team & Crunchbase")

    # Apify is deliberately not wired in: its actors bill per call, and the
    # console's scraping page no longer offers them. CompanyDataScraper skips
    # every Apify path when the token is empty.
    scraper = CompanyDataScraper(
        hasdata_api_key=hasdata_key,
        firecrawl_api_key=firecrawl_key,
    )

    company_name = _domain_to_name(url)
    log(f"  Running multi-API pipeline for: {company_name} ({url})")

    try:
        profile = scraper.scrape(company_name, url)
        row = _profile_to_row(profile, url)

        # Log a short summary
        conf = row.get("overall_confidence", "?")
        missing = row.get("missing_fields", "")
        log(f"  Confidence: {conf}  |  Missing: {missing or 'none'}")
        log(f"  Team: {len(profile.team_members)} members  |  Products: {len(profile.products)}  |  Funding rounds: {len(profile.funding_rounds)}")
        if profile.funding_rounds:
            log(f"  Total funding: ${profile.total_funding:,}" if profile.total_funding else "  Total funding: undisclosed")
        return row

    except Exception as e:
        log(f"  Multi-API pipeline failed: {e}")
        return None


# ───────────────────────────────────────────────────────────────────────
#  Selenium preview helper (visual only — data still comes from APIs)
# ───────────────────────────────────────────────────────────────────────

def _selenium_preview(driver, url: str, log: Callable, preview_cb: Optional[Callable]) -> None:
    """
    Load URL in the Selenium browser and send live screenshots to the UI.
    This is purely visual — it doesn't extract data.
    """
    from .scraper_core import _selenium_wait_with_live_preview
    try:
        driver.get(url)
        if preview_cb:
            log("  Live view: capturing page while scrolling…")
            _selenium_wait_with_live_preview(driver, 2.5, preview_cb, log=log)
        else:
            import time
            time.sleep(2.5)
    except Exception as e:
        log(f"  Selenium preview failed: {e}")


# ───────────────────────────────────────────────────────────────────────
#  Public entry point
# ───────────────────────────────────────────────────────────────────────

def run_scrape_headless(
    urls: List[str],
    use_selenium: bool,
    log_cb: Optional[Callable[[str], None]] = None,
    preview_cb: Optional[Callable[[str], None]] = None,
) -> tuple:
    """
    Scrape a list of URLs.  Returns (rows: list[dict], log_lines: list[str]).

    Data pipeline (per URL):
      1. Multi-API (HasData + Firecrawl + Apify) — rich structured data
         Falls back to → scraper_core if APIs unavailable or all fail

    Visual pipeline (use_selenium=True only):
      - Opens each URL in headless Chrome and streams live screenshots
      - Independent of the data pipeline — purely for the live-view UI
    """
    log_lines: List[str] = []

    def _log(msg: str) -> None:
        log_lines.append(msg)
        if log_cb:
            log_cb(msg)

    rows: List[Dict] = []
    driver = None

    # Start Chrome for the preview (visual only)
    if use_selenium and preview_cb:
        _log("Starting Chrome (Selenium, headless) for live preview…")
        from .scraper_core import get_selenium_driver
        driver = get_selenium_driver(headless=True, log=_log)
        if not driver:
            _log("Chrome failed to start — live preview disabled. Data pipeline continues.")

    try:
        for i, url in enumerate(urls, 1):
            url = url.strip()
            if not url or not url.startswith(("http://", "https://")):
                _log(f"Skipping invalid URL: {url[:80]}")
                continue

            _log(f"[{i}/{len(urls)}] Scraping: {url}")

            # Visual preview via Selenium (does not affect data)
            if driver and preview_cb:
                _selenium_preview(driver, url, _log, preview_cb)

            # ── Data: try rich pipeline first ──────────────────────────
            row = _scrape_rich(url, _log)

            # ── Fallback: basic website scrape ─────────────────────────
            if row is None:
                _log("  Falling back to basic website scraper…")
                if driver:
                    core_row = scrape_page_selenium(driver, url, wait_seconds=2.0, log=_log)
                else:
                    core_row = scrape_page_static(url, html=None, log=_log)
                core_row.pop("no_information", None)
                row = _fallback_to_row(core_row)

            _log(f"  → {row.get('company_name', '(no name)')}")
            _log("")
            rows.append(row)

        if not rows:
            _log("No data collected.")

    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass

    _log("Done.")
    return rows, log_lines
