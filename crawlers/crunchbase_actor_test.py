"""
Crunchbase Apify Actor Comparison Test
=======================================
Tests 6 Crunchbase actors against 10 companies.
Measures data quality, speed, cost, and reliability.

Usage:
    python -m crawlers.crunchbase_actor_test                  # full run
    python -m crawlers.crunchbase_actor_test --quick          # quick run
    python -m crawlers.crunchbase_actor_test --actor vulnv    # one actor
    python -m crawlers.crunchbase_actor_test --company openai # one company
"""

import argparse
import json
import os
import sys
import time
import datetime
import traceback

# Force UTF-8 output on Windows (avoids cp949 UnicodeEncodeError with emoji/box chars)
if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
from typing import Any, Dict, List, Optional, Tuple

import httpx

# ─────────────────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────────────────

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_env():
    """Read .env file from the project root."""
    env_path = os.path.join(PROJECT_ROOT, ".env")
    if not os.path.isfile(env_path):
        return
    with open(env_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip())

_load_env()
APIFY_TOKEN = os.environ.get("APIFY_API_TOKEN", "")
APIFY_BASE  = "https://api.apify.com/v2"

# ─────────────────────────────────────────────────────────────────────────────
# Test companies (slug → full URL)
# ─────────────────────────────────────────────────────────────────────────────

TEST_COMPANIES = [
    {"slug": "openai",        "name": "OpenAI",        "url": "https://www.crunchbase.com/organization/openai"},
    {"slug": "stripe",        "name": "Stripe",         "url": "https://www.crunchbase.com/organization/stripe"},
    {"slug": "anthropic",     "name": "Anthropic",      "url": "https://www.crunchbase.com/organization/anthropic"},
    {"slug": "databricks",    "name": "Databricks",     "url": "https://www.crunchbase.com/organization/databricks"},
    {"slug": "figma",         "name": "Figma",          "url": "https://www.crunchbase.com/organization/figma"},
    {"slug": "notion",        "name": "Notion",         "url": "https://www.crunchbase.com/organization/notion"},
    {"slug": "hugging-face",  "name": "Hugging Face",   "url": "https://www.crunchbase.com/organization/hugging-face"},
    {"slug": "scale-ai",      "name": "Scale AI",       "url": "https://www.crunchbase.com/organization/scale-ai"},
    {"slug": "cohere",        "name": "Cohere",         "url": "https://www.crunchbase.com/organization/cohere"},
    {"slug": "mistral-ai",    "name": "Mistral AI",     "url": "https://www.crunchbase.com/organization/mistral-ai"},
]

# ─────────────────────────────────────────────────────────────────────────────
# Actor registry
# Each entry: id, short name, input builder, notes
# ─────────────────────────────────────────────────────────────────────────────

def _std_start_urls(companies: List[dict]) -> dict:
    return {"startUrls": [{"url": c["url"]} for c in companies]}

def _davidsharadbhatt_input(companies: List[dict]) -> dict:
    # This actor only accepts empty input (uses its own default data/run mode)
    # Tested: startUrls, url, urls, companyUrl, query — all 400 "not allowed"
    # Empty {} → 201 OK. Actor seems to have hard-coded search behaviour.
    return {}

def _pratikdani_input(companies: List[dict]) -> dict:
    # Requires single "url" string (confirmed by 400 body testing)
    # Can only process one URL per run; we use the first company
    return {"url": companies[0]["url"] if companies else ""}

def _pratikdani_bulk_input_v2(companies: List[dict]) -> dict:
    # Requires "urls" as plain array of strings (confirmed by 400 body testing)
    return {"urls": [c["url"] for c in companies]}

def _saswave_input(companies: List[dict]) -> dict:
    # Search-based actor: pass slugs or names
    return {
        "queries": [c["name"] for c in companies],
        "maxResults": 1,
    }

def _pratikdani_bulk_input(companies: List[dict]) -> dict:
    return {
        "startUrls": [{"url": c["url"]} for c in companies],
        "maxConcurrency": 3,
    }

def _fundraising_input(companies: List[dict]) -> dict:
    return {
        "startUrls": [{"url": c["url"]} for c in companies],
        "includeFoundingRounds": True,
    }

ACTOR_REGISTRY = [
    {
        # Apify actor run URL uses ~ separator: /acts/username~actorName/runs
        "id":           "davidsharadbhatt~crunchbase-company-scraper",
        "short":        "davidsharadbhatt",
        "label":        "davidsharadbhatt/crunchbase-company-scraper",
        "input_fn":     _davidsharadbhatt_input,
        "est_cost_per_run_usd": 0.01,
        "notes":        "General company scraper, startUrls based",
    },
    {
        "id":           "vulnv~crunchbase-scraper-pro",
        "short":        "vulnv",
        "label":        "vulnv/crunchbase-scraper-pro",
        "input_fn":     _std_start_urls,
        "est_cost_per_run_usd": 0.02,
        "notes":        "Pro scraper, may handle anti-bot measures better",
    },
    {
        "id":           "saswave~crunchbase-search-results",
        "short":        "saswave",
        "label":        "saswave/crunchbase-search-results",
        "input_fn":     _saswave_input,
        "est_cost_per_run_usd": 0.005,
        "notes":        "Search-based; returns list items, less detail per company",
    },
    {
        "id":           "pratikdani~crunchbase-companies-bulk-scraper-no-cookies",
        "short":        "pratikdani-bulk",
        "label":        "pratikdani/crunchbase-companies-bulk-scraper-no-cookies",
        "input_fn":     _pratikdani_bulk_input_v2,
        "est_cost_per_run_usd": 0.01,
        "notes":        "Bulk, no-cookies approach — may work without Crunchbase login",
    },
    {
        "id":           "pratikdani~crunchbase-companies-scraper",
        "short":        "pratikdani",
        "label":        "pratikdani/crunchbase-companies-scraper",
        "input_fn":     _pratikdani_input,
        "est_cost_per_run_usd": 0.01,
        "notes":        "Standard Pratikdani scraper",
    },
    {
        "id":           "complex_intricate_networks~fundraising-and-startup-funding-scraper",
        "short":        "CIN-funding",
        "label":        "complex_intricate_networks/fundraising-and-startup-funding-scraper",
        "input_fn":     _fundraising_input,
        "est_cost_per_run_usd": 0.02,
        "notes":        "Funding-focused scraper",
    },
]

# ─────────────────────────────────────────────────────────────────────────────
# Canonical field schema + extractors
# Each extractor tries a list of field-name candidates
# ─────────────────────────────────────────────────────────────────────────────

def _first(*candidates, d: dict):
    for key in candidates:
        v = d.get(key)
        if v is not None and v != "" and v != [] and v != {}:
            return v
    return None

def _norm_str(v) -> Optional[str]:
    if v is None:
        return None
    if isinstance(v, list):
        return ", ".join(str(x) for x in v if x) or None
    s = str(v).strip()
    return s or None

def _norm_int(v) -> Optional[int]:
    if v is None:
        return None
    try:
        return int(str(v).replace(",", "").replace("+", "").split("-")[0].strip())
    except Exception:
        return None

def _norm_funding(v) -> Optional[float]:
    """Normalise funding to USD float."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).lower().replace(",", "").replace("$", "").replace("usd", "").strip()
    multipliers = {"b": 1e9, "m": 1e6, "k": 1e3}
    for suffix, mult in multipliers.items():
        if s.endswith(suffix):
            try:
                return float(s[:-1]) * mult
            except Exception:
                pass
    try:
        return float(s)
    except Exception:
        return None

def _extract_rounds(d: dict) -> Optional[List[dict]]:
    """Try to pull funding rounds from various actor output shapes."""
    raw = _first("fundingRounds", "funding_rounds", "rounds", "investments", d=d)
    if not raw or not isinstance(raw, list):
        return None
    rounds = []
    for r in raw:
        if not isinstance(r, dict):
            continue
        amount_raw = _first("raised_amount_usd", "amount", "raisedAmountUsd", "money_raised",
                             "moneyRaised", "fundingAmount", d=r)
        lead_inv = _first("lead_investors", "leadInvestors", "lead_investor", d=r)
        if isinstance(lead_inv, str):
            lead_inv = [lead_inv]
        elif not isinstance(lead_inv, list):
            lead_inv = []
        # lead investors may be a list of dicts with 'name' or plain strings
        lead_names = []
        for li in lead_inv:
            if isinstance(li, dict):
                lead_names.append(li.get("name") or li.get("value") or str(li))
            elif li:
                lead_names.append(str(li))
        rounds.append({
            "type":           _norm_str(_first("investment_type", "round_type", "type", "fundingType", d=r)),
            "date":           _norm_str(_first("announced_on", "date", "announcedDate", "fundingDate", d=r)),
            "amount":         _norm_funding(amount_raw),
            "lead_investors": lead_names,
        })
    return rounds or None

def _flatten_location_groups(d: dict) -> str:
    """vulnv stores location as location_groups list of dicts with 'name'."""
    raw = d.get("location_groups") or d.get("locations") or []
    if not isinstance(raw, list):
        return ""
    names = []
    for item in raw:
        if isinstance(item, dict):
            n = item.get("name") or item.get("value") or ""
            if n:
                names.append(str(n))
        elif isinstance(item, str) and item:
            names.append(item)
    return ", ".join(names)

def _flatten_cats(v) -> Optional[List[str]]:
    """Categories may be list of strings OR list of {id,value} dicts."""
    if not v:
        return None
    if isinstance(v, str):
        return [v]
    if not isinstance(v, list):
        return None
    result = []
    for item in v:
        if isinstance(item, dict):
            result.append(str(item.get("value") or item.get("name") or item.get("id") or ""))
        elif isinstance(item, str) and item:
            result.append(item)
    return [x for x in result if x] or None

def _extract_social(d: dict, *keys) -> Optional[str]:
    """Check top-level keys AND inside social_media/social_links sub-dicts."""
    top = _first(*keys, d=d)
    if top:
        return _norm_str(top)
    for container_key in ("social_media", "socials", "socialLinks", "social_links", "links"):
        container = d.get(container_key)
        if isinstance(container, dict):
            val = _first(*keys, d=container)
            if val:
                return _norm_str(val)
        elif isinstance(container, list):
            # List of {type, url} dicts (some actors use this format)
            for link in container:
                if isinstance(link, dict):
                    lt = str(link.get("type") or "").lower()
                    if any(k.lower().replace("_url","").replace("url","") in lt for k in keys):
                        return _norm_str(link.get("url") or link.get("href"))
    return None

def _norm_pratikdani(d: dict) -> dict:
    """
    Normalise the pratikdani Crunchbase scraper output.
    These actors return Crunchbase's internal page-data API structure with deeply
    nested sub-cards rather than flat key→value JSON.
    """
    # Name
    props = d.get("properties") or {}
    name = (props.get("identifier") or {}).get("value") or \
           (d.get("semrush_summary") or {}).get("identifier", {}).get("value")

    # Description
    desc       = (d.get("overview_description") or {}).get("description")
    short_desc = (d.get("about_short_description") or {}).get("short_description") or \
                 props.get("short_description")
    description = desc or short_desc

    # Contact
    contact = d.get("contact_fields") or {}
    email   = contact.get("contact_email")
    phone   = contact.get("phone_number")

    # Social
    social   = d.get("social_fields") or {}
    linkedin = (social.get("linkedin") or {}).get("value")
    twitter  = (social.get("twitter")  or {}).get("value")
    facebook = (social.get("facebook") or {}).get("value")

    # Funding summary
    fund_sum     = d.get("funding_rounds_summary") or {}
    fund_total   = (fund_sum.get("funding_total") or {}).get("value_usd") or \
                   (d.get("frequently_asked_questions_total_funding") or {}).get(
                       "funding_total", {}).get("value_usd")
    total_funding = _norm_funding(fund_total)

    # HQ Location
    hq_loc   = d.get("frequently_asked_questions_headquarters_location") or {}
    loc_ids  = hq_loc.get("location_identifiers") or []
    hq_parts = [l.get("value") for l in loc_ids if isinstance(l, dict) and l.get("value")]
    hq       = ", ".join(hq_parts) if hq_parts else None

    # Funding rounds
    rounds_raw = d.get("funding_rounds_list") or []
    rounds = []
    for r in (rounds_raw or []):
        if not isinstance(r, dict):
            continue
        amt_usd    = (r.get("money_raised") or {}).get("value_usd") or \
                     r.get("raised_amount_usd")
        lead_raw   = r.get("lead_investor_identifiers") or []
        lead_names = [li.get("value") for li in lead_raw if isinstance(li, dict) and li.get("value")]
        rounds.append({
            "type":           r.get("investment_type"),
            "date":           r.get("announced_on"),
            "amount":         _norm_funding(amt_usd),
            "lead_investors": lead_names,
        })

    # Team members
    team_raw = d.get("current_employees_image_list") or []
    team = []
    for t in team_raw:
        if not isinstance(t, dict):
            continue
        person = t.get("person_identifier") or {}
        team.append({
            "name":  person.get("value"),
            "title": t.get("title") or t.get("job_type"),
        })

    # Extended overview for location/categories/founded
    overview = d.get("overview_fields_extended") or {}

    # Founded year
    founded_raw = props.get("founded_on") or overview.get("founded_on")
    if isinstance(founded_raw, dict):
        founded_raw = founded_raw.get("value")
    founded = _norm_int(founded_raw)

    # Employee range
    emp_raw   = props.get("num_employees_enum") or overview.get("num_employees_enum")
    emp_range = (emp_raw.get("value") if isinstance(emp_raw, dict) else _norm_str(emp_raw))

    # Website
    website = props.get("homepage_url") or overview.get("homepage_url")

    # Categories
    cats_raw = props.get("categories") or overview.get("categories") or []
    cats = [c.get("value") for c in cats_raw if isinstance(c, dict) and c.get("value")] or None

    # Revenue range (privco or aberdeen data)
    rev = None
    for key in ("company_financials_highlights", "aberdeen_summary"):
        container = d.get(key) or {}
        if isinstance(container, dict):
            rev = _norm_str(container.get("revenue_range") or container.get("annual_revenue_range"))
            if rev:
                break

    return {
        "name":           name,
        "description":    description,
        "founded_year":   founded,
        "hq_location":    hq,
        "address":        None,
        "employee_count": None,
        "employee_range": emp_range,
        "website":        website,
        "email":          email,
        "phone":          phone,
        "industry":       cats[0] if cats else None,
        "categories":     cats,
        "revenue_range":  rev,
        "total_funding":  total_funding,
        "funding_rounds": rounds or None,
        "linkedin_url":   linkedin,
        "twitter_url":    twitter,
        "facebook_url":   facebook,
        "team_members":   team or None,
        "logo_url":       (props.get("identifier") or {}).get("image_id"),
        "cb_url":         None,
        "_error":         None,
        "_raw":           d,
    }


def normalise(raw: dict) -> dict:
    """Map any actor's raw output dict to a canonical schema."""
    # Skip error records (some actors emit {error: "...", error_code: "..."})
    if raw.get("error") and not raw.get("name") and not raw.get("company_name"):
        return {"_error": raw.get("error"), "_raw": raw}

    # Dispatch to specialised normaliser for pratikdani (Crunchbase internal API format)
    if "semrush_summary" in raw or "funding_rounds_summary" in raw and "contact_fields" in raw:
        return _norm_pratikdani(raw)

    d = raw

    # Location: try various patterns including vulnv's location_groups
    city    = _norm_str(_first("city", "hq_city", "headquarters_city", "location_city", d=d))
    country = _norm_str(_first("country_code", "country", "hq_country", "location_country", d=d))
    region  = _norm_str(_first("region", "state", "hq_region", d=d))
    # Try string-valued location fields first; skip if the value is a list/dict (requires special handling)
    for _lk in ("hq_location", "headquarters", "location", "city_name", "full_address"):
        _lv = d.get(_lk)
        if _lv and isinstance(_lv, str) and _lv.strip():
            hq_full = _lv.strip()
            break
    else:
        hq_full = None
    if not hq_full:
        # vulnv uses location_groups: [{name, permalink}, ...]
        hq_full = _flatten_location_groups(d) or None
    if not hq_full and city:
        hq_full = ", ".join(filter(None, [city, region, country]))

    # Funding
    total_raw = _first("total_funding_usd", "totalFunding", "total_funding",
                        "fundingTotal", "funding_total", "total_raised",
                        "funding_total_usd", "total_money_raised", d=d)
    total_usd = _norm_funding(total_raw)

    # Social links
    linkedin = _extract_social(d, "linkedin_url", "linkedin", "linkedinUrl")
    twitter  = _extract_social(d, "twitter_url",  "twitter",  "twitterUrl", "twitter_handle")
    facebook = _extract_social(d, "facebook_url", "facebook", "facebookUrl")

    # Categories / industry
    cats_raw = _first("categories", "industries", "industry_list", "verticals",
                       "category_list", "market", "sectors", d=d)
    cats     = _flatten_cats(cats_raw)
    industry = _norm_str(_first("industry", "primary_industry", "sector", d=d))
    if not industry and cats:
        industry = cats[0] if isinstance(cats[0], str) else None

    # Employee count — vulnv returns numeric employee_count
    emp_count = _norm_int(_first("num_employees", "employee_count", "employeeCount",
                                  "number_of_employees", "employees", d=d))
    emp_range = _norm_str(_first("employee_count_range", "employeeRange", "num_employees_enum",
                                  "company_size", "size", "headcount", d=d))

    # Phone — vulnv may use "phone_number"
    phone = _norm_str(_first("phone", "phone_number", "contact_phone", "tel", d=d))

    # Founded year — vulnv uses "founded_on" (date string "YYYY-MM-DD" or just "YYYY")
    founded = _norm_int(_first("founded_on", "founded_year", "foundedYear",
                                "founded_date", "yearFounded", "founded", d=d))

    # Website
    website = _norm_str(_first("homepage_url", "website", "website_url", "domain",
                                "primary_url", d=d))

    # Description — collect all candidates, prefer longest
    desc_candidates = [
        _norm_str(d.get("description")),
        _norm_str(d.get("long_description")),
        _norm_str(d.get("short_description")),
        _norm_str(d.get("about")),       # vulnv uses "about"
        _norm_str(d.get("overview")),
        _norm_str(d.get("intro")),
        _norm_str(d.get("tagline")),
    ]
    description = max((x for x in desc_candidates if x), key=len, default=None)

    return {
        "name":            _norm_str(_first("name", "company_name", "organization_name",
                                            "title", "legal_name", d=d)),
        "description":     description,
        "founded_year":    founded,
        "hq_location":     hq_full,
        "address":         _norm_str(_first("address", "full_address", "hq_address", d=d)),
        "employee_count":  emp_count,
        "employee_range":  emp_range,
        "website":         website,
        "email":           _norm_str(_first("email", "contact_email", "emails", d=d)),
        "phone":           phone,
        "industry":        industry,
        "categories":      cats,
        "revenue_range":   _norm_str(_first("revenue_range", "revenueRange", "revenue",
                                            "estimated_revenue", d=d)),
        "total_funding":   total_usd,
        "funding_rounds":  _extract_rounds(d),
        "linkedin_url":    linkedin,
        "twitter_url":     twitter,
        "facebook_url":    facebook,
        "team_members":    _first("team", "team_members", "people", "founders",
                                  "board_members", d=d),
        "logo_url":        _norm_str(_first("logo_url", "image_url", "logoUrl",
                                            "profile_image_url", "logo", d=d)),
        "cb_url":          _norm_str(_first("crunchbase_url", "cb_url", "permalink",
                                            "organization_url", d=d)),
        "_error":          None,
        "_raw":            raw,
    }

# ─────────────────────────────────────────────────────────────────────────────
# Scoring — for each field: 1=present+quality, 0.5=present+low quality, 0=missing
# ─────────────────────────────────────────────────────────────────────────────

SCORED_FIELDS = [
    "name", "description", "founded_year", "hq_location",
    "employee_count", "employee_range", "website",
    "email", "phone", "industry", "revenue_range",
    "total_funding", "funding_rounds", "linkedin_url",
    "twitter_url", "team_members",
]

def score_record(norm: dict) -> Tuple[float, dict]:
    """Return (0-100 score, per-field scores)."""
    scores = {}
    for field in SCORED_FIELDS:
        v = norm.get(field)
        if v is None:
            scores[field] = 0
        elif field == "description" and isinstance(v, str) and len(v) > 50:
            scores[field] = 1
        elif field == "description" and isinstance(v, str):
            scores[field] = 0.5
        elif field == "funding_rounds" and isinstance(v, list) and len(v) > 0:
            scores[field] = 1 if any(r.get("amount") for r in v) else 0.5
        elif field == "team_members" and isinstance(v, list) and len(v) > 0:
            scores[field] = 1
        elif v:
            scores[field] = 1
        else:
            scores[field] = 0

    total = sum(scores.values()) / len(SCORED_FIELDS) * 100
    return round(total, 1), scores

def score_symbol(s: float) -> str:
    if s >= 1:   return "✅"
    if s >= 0.5: return "⚠️ "
    return "❌"

# ─────────────────────────────────────────────────────────────────────────────
# Apify HTTP helpers
# ─────────────────────────────────────────────────────────────────────────────

def _apify_post(path: str, payload: dict, timeout: float = 60.0) -> dict:
    url = APIFY_BASE + path
    r = httpx.post(url, json=payload, params={"token": APIFY_TOKEN}, timeout=timeout)
    if not r.is_success:
        body = r.text[:500] if r.text else "(no body)"
        raise httpx.HTTPStatusError(
            f"HTTP {r.status_code} {r.url}\nBody: {body}",
            request=r.request, response=r
        )
    return r.json()

def _apify_get(path: str, params: Optional[dict] = None, timeout: float = 30.0) -> Any:
    url = APIFY_BASE + path
    p = {"token": APIFY_TOKEN}
    if params:
        p.update(params)
    r = httpx.get(url, params=p, timeout=timeout)
    r.raise_for_status()
    return r.json()

def run_actor_and_wait(actor_id: str, input_data: dict,
                       max_wait: int = 300, poll_interval: int = 8) -> Tuple[List[dict], dict]:
    """
    Start actor run, poll until done, return (items, run_stats).
    run_stats contains: run_id, status, compute_units, duration_secs
    """
    resp = _apify_post(f"/acts/{actor_id}/runs", input_data)
    run_data = resp.get("data") or {}
    run_id = run_data.get("id")
    if not run_id:
        raise ValueError(f"No run ID returned for {actor_id}: {resp}")

    waited = 0
    while waited < max_wait:
        time.sleep(poll_interval)
        waited += poll_interval
        status_resp = _apify_get(f"/actor-runs/{run_id}")
        status_data = status_resp.get("data") or {}
        state = status_data.get("status", "")

        if state == "SUCCEEDED":
            dataset_id = status_data.get("defaultDatasetId", "")
            stats = {
                "run_id":           run_id,
                "status":           state,
                "compute_units":    (status_data.get("stats") or {}).get("computeUnits", 0),
                "duration_secs":    waited,
                "dataset_id":       dataset_id,
            }
            items = []
            if dataset_id:
                result = _apify_get(f"/datasets/{dataset_id}/items",
                                    {"limit": 500, "format": "json"})
                items = result if isinstance(result, list) else result.get("items", [])
            return items, stats

        if state in ("FAILED", "ABORTED", "TIMED-OUT"):
            raise RuntimeError(f"Actor run {run_id} ended with status: {state}")

    raise TimeoutError(f"Actor run {run_id} timed out after {max_wait}s")

# ─────────────────────────────────────────────────────────────────────────────
# Test runner
# ─────────────────────────────────────────────────────────────────────────────

def test_actor(actor: dict, companies: List[dict]) -> dict:
    """
    Run one actor against the company list.
    Returns a result dict with raw items, normalised records, scores, timing.
    """
    actor_id   = actor["id"]
    input_data = actor["input_fn"](companies)

    print(f"\n  → Starting {actor['label']}")
    print(f"     Input: {json.dumps(input_data)[:120]}…")

    t0 = time.time()
    error = None
    items = []
    run_stats = {}

    try:
        items, run_stats = run_actor_and_wait(actor_id, input_data)
    except Exception as exc:
        error = str(exc)
        print(f"     ❌ FAILED: {error}")

    elapsed = time.time() - t0

    # Normalise each item — skip error records (items with "error" and no real data)
    normalised_all  = [normalise(item) for item in items]
    normalised      = [n for n in normalised_all if not n.get("_error")]
    error_records   = [n for n in normalised_all if n.get("_error")]
    if error_records:
        print(f"     Skipped {len(error_records)} error record(s) from actor output")

    # Score each normalised record
    scored = []
    for norm in normalised:
        sc, per_field = score_record(norm)
        scored.append({"norm": norm, "score": sc, "fields": per_field})

    avg_score = (sum(s["score"] for s in scored) / len(scored)) if scored else 0

    # Cost estimate: 0.25 USD per compute unit (Apify standard)
    cu  = run_stats.get("compute_units", 0) or 0
    cost_usd = cu * 0.25

    result = {
        "actor":         actor,
        "companies":     companies,
        "input":         input_data,
        "raw_items":     items,
        "normalised":    normalised,
        "scored":        scored,
        "avg_score":     round(avg_score, 1),
        "elapsed_secs":  round(elapsed, 1),
        "run_stats":     run_stats,
        "cost_usd":      round(cost_usd, 4),
        "error":         error,
        "item_count":    len(normalised),   # only valid (non-error) items
        "raw_count":     len(items),
    }

    if error:
        print(f"     Elapsed: {elapsed:.1f}s  |  Items: 0  |  Error: {error[:80]}")
    else:
        print(f"     Done -- {len(normalised)} valid items ({len(items)} raw) in {elapsed:.1f}s  "
              f"|  Score: {avg_score:.0f}/100  |  CUs: {cu:.3f}  |  Est: ${cost_usd:.4f}")

    return result

# ─────────────────────────────────────────────────────────────────────────────
# Reporting helpers
# ─────────────────────────────────────────────────────────────────────────────

DIVIDER     = "=" * 110
SUB_DIVIDER = "-" * 110

def _trunc(s: Any, n: int = 60) -> str:
    if s is None:
        return "—"
    s = str(s)
    return s[:n] + "…" if len(s) > n else s

def _money(v: Optional[float]) -> str:
    if v is None:
        return "—"
    if v >= 1e9:
        return f"${v/1e9:.1f}B"
    if v >= 1e6:
        return f"${v/1e6:.0f}M"
    if v >= 1e3:
        return f"${v/1e3:.0f}K"
    return f"${v:.0f}"

def print_summary_table(results: List[dict]):
    print(f"\n{DIVIDER}")
    print("  SUMMARY COMPARISON TABLE")
    print(DIVIDER)

    # Header
    col = 35
    fields_short = ["name", "desc", "founded", "hq", "emp", "web",
                    "email", "phone", "industry", "rev", "funding", "rounds",
                    "linkedin", "twitter", "team"]

    print(f"{'Actor':<35} | {'Items':>5} | {'Score':>6} | {'Time':>6} | {'Cost':>7} | " +
          " | ".join(f"{f[:7]:>7}" for f in fields_short))
    print(SUB_DIVIDER)

    for res in results:
        label  = res["actor"]["short"]
        n      = res["item_count"]
        score  = res["avg_score"]
        t      = res["elapsed_secs"]
        cost   = res["cost_usd"]
        err    = res["error"]

        if err:
            print(f"{label:<35} | {'—':>5} | {'ERROR':>6} | {t:>5.0f}s | {'—':>7} | "
                  + "   ❌    " * len(fields_short))
            continue

        # Average field scores across all records
        field_avgs = {}
        for field in SCORED_FIELDS:
            vals = [s["fields"].get(field, 0) for s in res["scored"]]
            field_avgs[field] = sum(vals) / len(vals) if vals else 0

        field_map = {
            "name":      field_avgs.get("name", 0),
            "desc":      field_avgs.get("description", 0),
            "founded":   field_avgs.get("founded_year", 0),
            "hq":        field_avgs.get("hq_location", 0),
            "emp":       max(field_avgs.get("employee_count", 0),
                             field_avgs.get("employee_range", 0)),
            "web":       field_avgs.get("website", 0),
            "email":     field_avgs.get("email", 0),
            "phone":     field_avgs.get("phone", 0),
            "industry":  field_avgs.get("industry", 0),
            "rev":       field_avgs.get("revenue_range", 0),
            "funding":   field_avgs.get("total_funding", 0),
            "rounds":    field_avgs.get("funding_rounds", 0),
            "linkedin":  field_avgs.get("linkedin_url", 0),
            "twitter":   field_avgs.get("twitter_url", 0),
            "team":      field_avgs.get("team_members", 0),
        }

        cells = " | ".join(f"{score_symbol(field_map.get(f,0)):>7}" for f in fields_short)
        print(f"{label:<35} | {n:>5} | {score:>5.0f}% | {t:>5.0f}s | ${cost:>6.4f} | {cells}")

    print(f"\nLegend: ✅ = Extracted  ⚠️  = Partial  ❌ = Missing")

def print_actor_detail(res: dict):
    actor = res["actor"]
    print(f"\n{DIVIDER}")
    print(f"  ACTOR: {actor['label']}")
    print(f"  Notes: {actor['notes']}")
    print(DIVIDER)

    if res["error"]:
        print(f"  ❌ ERROR: {res['error']}")
        print(f"  Elapsed: {res['elapsed_secs']:.1f}s")
        return

    print(f"  Items returned : {res['item_count']}")
    print(f"  Elapsed        : {res['elapsed_secs']:.1f}s")
    print(f"  Compute units  : {res['run_stats'].get('compute_units', '?'):.4f}")
    print(f"  Est. cost      : ${res['cost_usd']:.4f}")
    print(f"  Average score  : {res['avg_score']:.0f}/100")

    # Per-record detail
    for i, scored in enumerate(res["scored"]):
        norm = scored["norm"]
        sc   = scored["score"]
        fields = scored["fields"]
        print(f"\n  -- Company {i+1}: {norm.get('name') or '(unnamed)'} -- Score: {sc:.0f}/100 --")

        rows = [
            ("Name",          norm.get("name")),
            ("Description",   _trunc(norm.get("description"), 100)),
            ("Founded",       norm.get("founded_year")),
            ("HQ Location",   norm.get("hq_location")),
            ("Address",       norm.get("address")),
            ("Employees",     norm.get("employee_count") or norm.get("employee_range")),
            ("Website",       norm.get("website")),
            ("Email",         norm.get("email")),
            ("Phone",         norm.get("phone")),
            ("Industry",      norm.get("industry")),
            ("Revenue range", norm.get("revenue_range")),
            ("Total funding", _money(norm.get("total_funding"))),
            ("LinkedIn",      norm.get("linkedin_url")),
            ("Twitter",       norm.get("twitter_url")),
        ]
        for label, val in rows:
            sym = "✅" if val and str(val) != "—" else "❌"
            print(f"    {sym}  {label:<18} {_trunc(val, 70)}")

        rounds = norm.get("funding_rounds") or []
        if rounds:
            print(f"    ✅  Funding rounds  ({len(rounds)} found):")
            for r in rounds[:5]:
                print(f"          • {r.get('type','?')} — {r.get('date','?')} — "
                      f"{_money(r.get('amount'))} — "
                      f"Lead: {', '.join(r.get('lead_investors') or []) or '?'}")
        else:
            print(f"    ❌  Funding rounds  (none found)")

        team = norm.get("team_members") or []
        if isinstance(team, list) and team:
            print(f"    ✅  Team members   ({len(team)} found):")
            for m in team[:3]:
                if isinstance(m, dict):
                    print(f"          • {m.get('name','?')} — {m.get('title','?')}")
        else:
            print(f"    ❌  Team members   (none found)")

    # Pros/cons
    sc = res["avg_score"]
    print(f"\n  -- Pros & Cons --")
    if sc >= 70:
        print(f"  👍 Strong overall data quality ({sc:.0f}/100)")
    elif sc >= 40:
        print(f"  ⚠️  Moderate data quality ({sc:.0f}/100) — usable but incomplete")
    else:
        print(f"  👎 Low data quality ({sc:.0f}/100) — consider combining with other sources")

    avg_fields = {}
    for field in SCORED_FIELDS:
        vals = [s["fields"].get(field, 0) for s in res["scored"]]
        avg_fields[field] = sum(vals) / len(vals) if vals else 0

    strong = [f for f, v in avg_fields.items() if v >= 0.9]
    weak   = [f for f, v in avg_fields.items() if v < 0.3]
    print(f"  ✅ Strong fields: {', '.join(strong) or 'none'}")
    print(f"  ❌ Weak fields  : {', '.join(weak) or 'none'}")

def print_cost_analysis(results: List[dict], companies_tested: int):
    print(f"\n{DIVIDER}")
    print("  COST ANALYSIS (Extrapolated from test run)")
    print(DIVIDER)
    print(f"  {'Actor':<35} | {'Per run':>9} | {'100 cos':>9} | {'1K cos':>9} | {'10K cos':>9}")
    print(SUB_DIVIDER)

    for res in results:
        label = res["actor"]["short"]
        if res["error"] or not res["item_count"]:
            print(f"  {label:<35} | {'ERROR':>9} | {'—':>9} | {'—':>9} | {'—':>9}")
            continue
        # Cost per company = total cost / companies returned
        n = res["item_count"]
        cost_per_company = res["cost_usd"] / n if n > 0 else res["actor"]["est_cost_per_run_usd"]
        c100   = cost_per_company * 100
        c1000  = cost_per_company * 1000
        c10000 = cost_per_company * 10000
        print(f"  {label:<35} | ${res['cost_usd']:>8.4f} | ${c100:>8.2f} | ${c1000:>8.2f} | ${c10000:>8.2f}")

def print_recommendations(results: List[dict]):
    print(f"\n{DIVIDER}")
    print("  RECOMMENDATIONS")
    print(DIVIDER)

    # Filter working actors
    working = [r for r in results if not r["error"] and r["item_count"] > 0]
    if not working:
        print("  ⚠️  No actors returned results. Check API token and actor availability.")
        return

    def rank_by(key):
        """Sort working results by a key function."""
        if key == "score":
            return sorted(working, key=lambda r: r["avg_score"], reverse=True)
        if key == "funding":
            return sorted(working, key=lambda r: sum(
                s["fields"].get("total_funding", 0) + s["fields"].get("funding_rounds", 0)
                for s in r["scored"]
            ) / max(len(r["scored"]), 1), reverse=True)
        if key == "speed":
            return sorted(working, key=lambda r: r["elapsed_secs"])
        if key == "cost":
            def cost_per(r):
                n = r["item_count"]
                return r["cost_usd"] / n if n > 0 else 9999
            return sorted(working, key=cost_per)
        return working

    by_score   = rank_by("score")
    by_funding = rank_by("funding")
    by_speed   = rank_by("speed")
    by_cost    = rank_by("cost")

    def best(lst, reason):
        r = lst[0]
        print(f"  ● {reason}")
        print(f"      → {r['actor']['label']}")
        sc = r['avg_score']
        t  = r['elapsed_secs']
        n  = r['item_count']
        print(f"         Score: {sc:.0f}/100  |  {t:.0f}s  |  {n} items  |  ${r['cost_usd']:.4f}")

    best(by_score,   "Best overall (highest data quality score):")
    best(by_funding, "Best for funding data:")
    best(by_speed,   "Fastest:")
    best(by_cost,    "Most cost-efficient (per company):")

    print(f"\n  -- Combination Recommendation --")
    if len(by_score) >= 2:
        print(f"  For maximum coverage, combine:")
        print(f"    1. {by_score[0]['actor']['label']}  (primary — best overall)")
        print(f"    2. {by_funding[0]['actor']['label']}  (supplement for funding data)")
        print(f"  This covers gaps that single actors miss (e.g. email vs. funding rounds).")

    print(f"\n  -- Integration Code Snippets --")
    for res in by_score[:2]:
        actor = res["actor"]
        print(f"\n  # {actor['label']}")
        print(f"  client = ApifyClient(api_token=APIFY_TOKEN)")
        sample_input = json.dumps(actor["input_fn"]([TEST_COMPANIES[0]]), indent=2)
        for line in sample_input.split("\n"):
            print(f"  # {line}")
        print(f"  items = client.run_actor('{actor['id']}', input_data)")
        print(f"  # Normalise with: norm = normalise(items[0])")

def print_raw_samples(results: List[dict], max_chars: int = 800):
    print(f"\n{DIVIDER}")
    print("  RAW OUTPUT SAMPLES (first item per actor, truncated)")
    print(DIVIDER)
    for res in results:
        print(f"\n  -- {res['actor']['label']} --")
        if res["error"]:
            print(f"  ERROR: {res['error']}")
            continue
        if not res["raw_items"]:
            print("  (no items returned)")
            continue
        sample = json.dumps(res["raw_items"][0], indent=2, ensure_ascii=False)
        if len(sample) > max_chars:
            sample = sample[:max_chars] + "\n  … (truncated)"
        print(sample)

# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Crunchbase Apify actor comparison test")
    parser.add_argument("--quick",   action="store_true", help="1 company, first 3 actors")
    parser.add_argument("--actor",   help="Run only actor whose short name contains this string")
    parser.add_argument("--company", action="append", help="Run only company whose slug contains this string (repeat for multiple)")
    parser.add_argument("--max-wait",type=int, default=300, help="Max seconds per actor run (default 300)")
    parser.add_argument("--no-raw",  action="store_true", help="Skip raw output section")
    args = parser.parse_args()

    if not APIFY_TOKEN:
        print("ERROR: APIFY_API_TOKEN not set. Add it to .env or set as environment variable.")
        sys.exit(1)

    # Select companies
    companies = TEST_COMPANIES
    if args.company:
        companies = [c for c in companies if args.company.lower() in c["slug"].lower()]
        if not companies:
            print(f"No company matching '{args.company}'")
            sys.exit(1)
    if args.quick:
        companies = companies[:1]

    # Select actors
    actors = ACTOR_REGISTRY
    if args.actor:
        actors = [a for a in actors if args.actor.lower() in a["short"].lower() or args.actor.lower() in a["id"].lower()]
        if not actors:
            print(f"No actor matching '{args.actor}'")
            sys.exit(1)
    if args.quick:
        actors = actors[:3]

    print(DIVIDER)
    print(f"  CRUNCHBASE APIFY ACTOR COMPARISON TEST")
    print(f"  Date   : {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"  Actors : {len(actors)}")
    print(f"  Companies: {len(companies)} — {', '.join(c['name'] for c in companies)}")
    print(DIVIDER)

    results = []
    for actor in actors:
        res = test_actor(actor, companies)
        results.append(res)

    # Reports
    print_summary_table(results)
    for res in results:
        print_actor_detail(res)
    print_cost_analysis(results, len(companies))
    print_recommendations(results)
    if not args.no_raw:
        print_raw_samples(results)

    # Save full results to JSON
    out_path = os.path.join(PROJECT_ROOT,
                            f"crunchbase_test_results_{datetime.datetime.now().strftime('%Y%m%d_%H%M')}.json")
    save_data = []
    for res in results:
        save_data.append({
            "actor":        res["actor"]["label"],
            "item_count":   res["item_count"],
            "avg_score":    res["avg_score"],
            "elapsed_secs": res["elapsed_secs"],
            "cost_usd":     res["cost_usd"],
            "error":        res["error"],
            "run_stats":    res["run_stats"],
            "normalised":   res["normalised"],
            "raw_items":    res["raw_items"],
        })
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(save_data, f, indent=2, ensure_ascii=False, default=str)

    print(f"\n{DIVIDER}")
    print(f"  Results saved to: {out_path}")
    print(DIVIDER)


if __name__ == "__main__":
    main()
