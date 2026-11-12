"""DirectoryAndNewsScraper — scrapes directories and news for company data."""
import re
import logging
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# Domains that carry useful company data in SERP results
TRUSTED_NEWS_DOMAINS = frozenset({
    "techcrunch.com", "crunchbase.com", "reuters.com", "bloomberg.com",
    "forbes.com", "businesswire.com", "prnewswire.com", "venturebeat.com",
})

# Domains to skip when looking for a company's official website
DIRECTORY_DOMAINS = frozenset({
    "linkedin.com", "crunchbase.com", "bloomberg.com",
    "wikipedia.org", "yelp.com", "facebook.com", "twitter.com",
    "instagram.com", "youtube.com", "glassdoor.com", "indeed.com",
    "zoominfo.com", "dnb.com", "pitchbook.com",
})


class DirectoryAndNewsScraper:
    """
    Collects company data from Google SERP, Google Maps, LinkedIn, and news:

    - SERP (HasData)    → knowledge panel fields: founded, HQ, size, industry
    - Maps (HasData)    → verified address, phone, website
    - LinkedIn (Apify)  → description, industry, employee count, founded year
    - News (HasData)    → recent announcements, funding, press releases

    Returns four separate dicts keyed by source so DataMerger can weight them.
    """

    def __init__(self, hasdata_client=None, firecrawl_client=None, apify_client=None):
        self._hasdata   = hasdata_client
        self._firecrawl = firecrawl_client
        self._apify     = apify_client

    # ------------------------------------------------------------------ #
    #  HasData: SERP knowledge panel                                       #
    # ------------------------------------------------------------------ #

    def _serp_company_info(self, company_name: str, url: str = "") -> Dict:
        if not self._hasdata:
            return {}
        try:
            # Bare company name triggers knowledge graph most reliably
            query = company_name
            result = self._hasdata.serp_search(query, num_results=10)
            # HasData returns camelCase keys; support both for resilience
            kg = (result.get("knowledgeGraph") or result.get("knowledge_graph") or {})
            out: Dict = {}

            # Knowledge panel fields (vary by company — map common keys)
            if kg.get("title"):
                out["name"] = kg["title"]
            desc = kg.get("description") or kg.get("snippet") or ""
            if desc:
                out["introduction"] = desc
            founded_raw = kg.get("founded") or kg.get("foundedDate") or ""
            if founded_raw:
                year_m = re.search(r"\d{4}", str(founded_raw))
                if year_m:
                    out["founded_year"] = int(year_m.group(0))
            hq = kg.get("headquarters") or kg.get("headquartersLocation") or ""
            if hq:
                out["hq_location"] = hq
            size_raw = (kg.get("employees") or kg.get("numberOfEmployees")
                        or kg.get("number_of_employees") or "")
            if size_raw:
                out["size"] = str(size_raw)
            industry = kg.get("type") or kg.get("industry") or kg.get("industryType") or ""
            if industry:
                out["industry"] = industry
            if kg.get("website"):
                out["url"] = kg["website"]

            # Fall back to organic results when knowledge panel is sparse
            organic = (result.get("organicResults") or result.get("organic_results") or [])
            all_snippets = " ".join(item.get("snippet", "") for item in organic)

            if not out.get("introduction") and organic:
                # Use first relevant snippet as introduction
                for item in organic:
                    snippet = item.get("snippet", "")
                    if snippet and company_name.lower() in snippet.lower():
                        out["introduction"] = snippet[:500]
                        break
                if not out.get("introduction") and organic:
                    out["introduction"] = (organic[0].get("snippet") or "")[:500]

            if not out.get("founded_year") and all_snippets:
                year_m2 = re.search(
                    r'(?:founded|established|incorporated|since)\s+(?:in\s+)?(\d{4})',
                    all_snippets, re.IGNORECASE
                )
                if year_m2:
                    year = int(year_m2.group(1))
                    if 1900 <= year <= 2030:
                        out["founded_year"] = year

            if not out.get("hq_location") and all_snippets:
                hq_m = re.search(
                    r'(?:based|headquartered|located)\s+in\s+([A-Za-z][A-Za-z\s,]{3,50}?)(?:\.|,|\s)',
                    all_snippets, re.IGNORECASE
                )
                if hq_m:
                    out["hq_location"] = hq_m.group(1).strip().rstrip(",")

            if not out.get("industry") and all_snippets:
                industry_m = re.search(
                    r'(?:robotics|medtech|healthcare|software|fintech|biotech|edtech|logistics|'
                    r'e-commerce|ecommerce|manufacturing|energy|agriculture|real estate|'
                    r'ai|artificial intelligence|machine learning|saas|b2b|b2c)',
                    all_snippets, re.IGNORECASE
                )
                if industry_m:
                    out["industry"] = industry_m.group(0).title()

            # LinkedIn URL from organic results (HasData uses 'link' key)
            if not out.get("linkedin_url"):
                for item in organic:
                    link = item.get("link") or item.get("url") or ""
                    if "linkedin.com/company/" in link:
                        out["linkedin_url"] = link.split("?")[0]
                        break

            return out
        except Exception as e:
            logger.error("SERP company info failed for %r: %s", company_name, e)
            return {}

    # ------------------------------------------------------------------ #
    #  HasData: Google Maps                                                #
    # ------------------------------------------------------------------ #

    def _maps_company_info(self, company_name: str, location: str = "") -> Dict:
        if not self._hasdata:
            return {}
        try:
            result = self._hasdata.maps_search(company_name, location)
            items = result.get("local_results", [])
            if not items:
                return {}
            top = items[0]
            out: Dict = {}
            if top.get("title"):
                out["name"] = top["title"]
            if top.get("address"):
                out["address"] = top["address"]
            if top.get("phone"):
                out["phone"] = top["phone"]
            if top.get("website"):
                out["url"] = top["website"]
            # Maps often provides a structured service area or city
            if top.get("city") or top.get("state"):
                parts = [p for p in [top.get("city"), top.get("state"), top.get("country")] if p]
                if parts:
                    out["hq_location"] = ", ".join(parts)
            # Rating and review count (verified GMB data)
            if top.get("rating") is not None:
                out["rating"] = top["rating"]
            rc = top.get("reviews_count") or top.get("user_ratings_total") or top.get("reviews") or 0
            if rc:
                out["review_count"] = int(rc)
            return out
        except Exception as e:
            logger.error("Maps company info failed for %r: %s", company_name, e)
            return {}

    # ------------------------------------------------------------------ #
    #  LinkedIn Company: Firecrawl AI extraction                          #
    # ------------------------------------------------------------------ #

    # Schema for Firecrawl AI extraction on LinkedIn company pages
    _LI_SCHEMA: Dict = {
        "type": "object",
        "properties": {
            "name":         {"type": "string",  "description": "Company legal or brand name"},
            "tagline":      {"type": "string",  "description": "Short company tagline or slogan"},
            "introduction": {"type": "string",  "description": "Company description or about text"},
            "size":         {"type": "string",  "description": "Employee count or range"},
            "founded_year": {"type": "integer", "description": "Year the company was founded"},
            "industry":     {"type": "string",  "description": "Primary industry"},
            "hq_location":  {"type": "string",  "description": "Headquarters city and country"},
            "url":          {"type": "string",  "description": "Company website URL"},
            "tags":         {"type": "array",   "items": {"type": "string"},
                             "description": "Specialties or tags listed on the page"},
        },
    }
    _LI_PROMPT = (
        "Extract structured company information from this LinkedIn company page. "
        "Return every field you can find. Use null for fields not present."
    )

    def _linkedin_company_info(self, company_name: str, linkedin_url: str = "") -> Dict:
        """Scrape LinkedIn company page via Firecrawl AI extraction."""
        if not self._firecrawl or not linkedin_url:
            return {}
        try:
            result = self._firecrawl.scrape_url(
                linkedin_url,
                formats=["extract"],
                only_main_content=True,
                extract_schema=self._LI_SCHEMA,
                extract_prompt=self._LI_PROMPT,
            )
            data = result.get("data") or result
            extracted = data.get("extract") or {}
            if not extracted:
                return {}

            out: Dict = {}
            _NULL = frozenset({"null", "none", "n/a", "unknown", ""})

            def _v(key):
                val = extracted.get(key)
                if val is None:
                    return None
                if isinstance(val, str) and val.lower().strip() in _NULL:
                    return None
                return val

            if _v("name"):         out["name"]         = _v("name")
            if _v("tagline"):      out["tagline"]      = _v("tagline")
            if _v("introduction"): out["introduction"] = _v("introduction")
            if _v("size"):         out["size"]         = str(_v("size"))
            if _v("founded_year"):
                try:
                    out["founded_year"] = int(_v("founded_year"))
                except (TypeError, ValueError):
                    pass
            if _v("industry"):     out["industry"]     = _v("industry")
            if _v("hq_location"):  out["hq_location"]  = _v("hq_location")
            if _v("url"):          out["url"]          = _v("url")
            tags = _v("tags")
            if isinstance(tags, list) and tags:
                out["tags"] = [t for t in tags if t and str(t).lower().strip() not in _NULL][:10]
            out["linkedin_url"] = linkedin_url.split("?")[0]
            return out
        except Exception as e:
            logger.error("LinkedIn Firecrawl extraction failed for %r: %s", company_name, e)
            return {}

    # ------------------------------------------------------------------ #
    #  HasData: targeted LinkedIn company URL search                       #
    # ------------------------------------------------------------------ #

    def _find_linkedin_url(self, company_name: str) -> str:
        """Search SERP for the company's LinkedIn page URL directly."""
        if not self._hasdata:
            return ""
        try:
            result = self._hasdata.serp_search(
                f'site:linkedin.com/company "{company_name}"', num_results=5
            )
            organic = result.get("organicResults") or result.get("organic_results") or []
            for item in organic:
                link = item.get("link") or item.get("url") or ""
                if re.search(r'linkedin\.com/company/', link, re.IGNORECASE):
                    return link.split("?")[0]
        except Exception as e:
            logger.debug("LinkedIn URL search failed for %r: %s", company_name, e)
        return ""

    # ------------------------------------------------------------------ #
    #  HasData: News search                                                #
    # ------------------------------------------------------------------ #

    def _scrape_news(self, company_name: str) -> List[Dict]:
        if not self._hasdata:
            return []
        try:
            result = self._hasdata.serp_search(
                f'"{company_name}" announcement funding partnership 2025 2026',
                num_results=10,
            )
            articles: List[Dict] = []
            organic_news = result.get("organicResults") or result.get("organic_results") or []
            for item in organic_news:
                link = item.get("link") or item.get("url") or ""
                domain_m = re.search(r"https?://(?:www\.)?([^/]+)", link)
                domain = domain_m.group(1) if domain_m else ""
                articles.append({
                    "title":   item.get("title", ""),
                    "snippet": item.get("snippet", ""),
                    "url":     link,
                    "source":  domain,
                    "date":    item.get("date", ""),
                    "trusted": domain in TRUSTED_NEWS_DOMAINS,
                })
            return articles
        except Exception as e:
            logger.error("News scraping failed for %r: %s", company_name, e)
            return []

    # ------------------------------------------------------------------ #
    #  URL discovery helper                                                #
    # ------------------------------------------------------------------ #

    def discover_url(self, company_name: str) -> str:
        """Return best-guess official website for a company name."""
        if not self._hasdata:
            return ""
        try:
            result = self._hasdata.serp_search(
                f"{company_name} official website", num_results=5
            )
            kg = result.get("knowledgeGraph") or result.get("knowledge_graph") or {}
            if kg.get("website"):
                return kg["website"]
            organic = result.get("organicResults") or result.get("organic_results") or []
            for item in organic:
                link = item.get("link") or item.get("url") or ""
                domain_m = re.search(r"https?://(?:www\.)?([^/]+)", link)
                if domain_m and domain_m.group(1) not in DIRECTORY_DOMAINS:
                    return link
        except Exception as e:
            logger.error("URL discovery failed for %r: %s", company_name, e)
        return ""

    # ------------------------------------------------------------------ #
    #  Public entry point                                                  #
    # ------------------------------------------------------------------ #

    def scrape(
        self, company_name: str, url: str = "", location: str = ""
    ) -> Dict[str, object]:
        """
        Run all directory/news sources in sequence.
        Returns:
          {
            "serp":     {...},   # HasData knowledge panel
            "maps":     {...},   # HasData Maps contact info
            "linkedin": {...},   # Apify LinkedIn company
            "news":     [...],   # list of article dicts
          }
        """
        logger.info("Directory/news scrape: %r", company_name)
        serp = self._serp_company_info(company_name, url)

        # Find LinkedIn URL: prefer SERP knowledge panel, then targeted search
        linkedin_url = serp.get("linkedin_url", "")
        if not linkedin_url:
            linkedin_url = self._find_linkedin_url(company_name)
            if linkedin_url:
                serp["linkedin_url"] = linkedin_url

        return {
            "serp":     serp,
            "maps":     self._maps_company_info(company_name, location),
            "linkedin": self._linkedin_company_info(company_name, linkedin_url),
            "news":     self._scrape_news(company_name),
        }
