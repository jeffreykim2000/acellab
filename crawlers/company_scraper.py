"""
CompanyDataScraper — main orchestrator for multi-source company data collection.

Usage:
    from crawlers.company_scraper import CompanyDataScraper

    scraper = CompanyDataScraper(
        hasdata_api_key="your_key",
        firecrawl_api_key="",
        apify_api_token="your_token",
    )
    profile = scraper.scrape("Stripe", "https://stripe.com")
    result  = profile.to_dict()    # full company object with accuracy metadata
"""
import logging
import os
import re
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin, urlparse

from .hasdata_client         import HasDataClient
from .firecrawl_client       import FirecrawlClient
from .apify_client           import ApifyClient
from .data_validator         import DataValidator
from .data_merger            import DataMerger
from .team_extractor         import TeamMemberExtractor
from .product_extractor      import ProductExtractor
from .funding_extractor      import FundingExtractor
from .directory_news_scraper import DirectoryAndNewsScraper
from .scrape_learner import ScrapeLearner, write_scrape_log

logger = logging.getLogger(__name__)

_learner = ScrapeLearner()


# ------------------------------------------------------------------ #
#  CompanyProfile dataclass                                            #
# ------------------------------------------------------------------ #

@dataclass
class CompanyProfile:
    # Identity
    name:         str = ""
    tags:         List[str] = field(default_factory=list)
    introduction: str = ""
    industry:     str = ""
    size:         str = ""
    founded_year: Optional[int] = None
    hq_location:  str = ""
    address:      str = ""
    linkedin_url: str = ""

    # Contact
    email: str = ""
    phone: str = ""
    url:   str = ""

    # Branding
    logo_url: str = ""
    tagline:  str = ""

    # Products & pricing
    products:      List[Dict] = field(default_factory=list)
    pricing_model: str = ""
    revenue_range: str = ""

    # Google Maps signals
    rating:       Optional[float] = None
    review_count: Optional[int]   = None

    # Team
    team_members: List[Dict] = field(default_factory=list)

    # Funding
    funding_rounds: List[Dict] = field(default_factory=list)
    total_funding:  Optional[int] = None
    investors:      List[str] = field(default_factory=list)

    # News (from HasData SERP)
    news: List[Dict] = field(default_factory=list)

    # Accuracy metadata (prefixed _ so they don't pollute display)
    _validation:   Optional[Dict] = field(default=None, repr=False)
    _merge_report: Optional[Dict] = field(default=None, repr=False)
    _source_map:   Optional[Dict] = field(default=None, repr=False)
    _sources_used: List[str]      = field(default_factory=list, repr=False)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name":          self.name,
            "tags":          self.tags,
            "introduction":  self.introduction,
            "industry":      self.industry,
            "size":          self.size,
            "founded_year":  self.founded_year,
            "hq_location":   self.hq_location,
            "address":       self.address,
            "linkedin_url":  self.linkedin_url,
            "email":         self.email,
            "phone":         self.phone,
            "url":           self.url,
            "logo_url":      self.logo_url,
            "tagline":       self.tagline,
            "products":      self.products,
            "pricing_model": self.pricing_model,
            "revenue_range": self.revenue_range,
            "rating":        self.rating,
            "review_count":  self.review_count,
            "team_members":  self.team_members,
            "funding_rounds": self.funding_rounds,
            "total_funding": self.total_funding,
            "investors":     self.investors,
            "news":          self.news,
            "_validation":   self._validation,
            "_merge_report": self._merge_report,
            "_source_map":   self._source_map,
            "_sources_used": self._sources_used,
        }


# ------------------------------------------------------------------ #
#  Main scraper class                                                  #
# ------------------------------------------------------------------ #

class CompanyDataScraper:
    """
    Orchestrates HasData → Firecrawl → Apify to build a complete CompanyProfile.

    Pipeline:
    1. Discover canonical URL (HasData SERP) if not provided
    2. Run HasData pipeline   — SERP knowledge panel + Maps + LinkedIn
    3. Run Firecrawl pipeline — website homepage scrape (intro, email, links)
    4. Run Apify pipeline     — LinkedIn team search
       (steps 2-4 run in parallel via ThreadPoolExecutor)
    5. Run ProductExtractor   — product pages via Firecrawl
    6. Run FundingExtractor   — Crunchbase + SERP (sequential, use discovered URL)
    7. Merge all results with DataMerger (weighted consensus)
    8. Validate with DataValidator (confidence scores, conflict detection)
    9. Return CompanyProfile
    """

    def __init__(
        self,
        hasdata_api_key:    str = "",
        firecrawl_api_key:  str = "",
        apify_api_token:    str = "",
        max_workers:        int = 3,
        custom_source_weights: Optional[Dict] = None,
    ):
        self._hasdata   = HasDataClient(hasdata_api_key)   if hasdata_api_key   else None
        self._firecrawl = FirecrawlClient(firecrawl_api_key) if firecrawl_api_key else None
        self._apify     = ApifyClient(apify_api_token)     if apify_api_token   else None
        self._workers   = max_workers

        # Sub-components
        self._merger    = DataMerger(custom_weights=custom_source_weights)
        self._validator = DataValidator()
        self._directory = DirectoryAndNewsScraper(self._hasdata, self._firecrawl, self._apify)
        self._teams     = TeamMemberExtractor(self._apify, self._firecrawl)
        self._products  = ProductExtractor(self._firecrawl, self._apify)
        self._funding   = FundingExtractor(self._hasdata, self._firecrawl, self._apify)

    # ------------------------------------------------------------------ #
    #  Step 1: URL discovery                                               #
    # ------------------------------------------------------------------ #

    def _discover_url(self, company_name: str, hint: str = "") -> str:
        if hint:
            return hint
        return self._directory.discover_url(company_name)

    # ------------------------------------------------------------------ #
    #  Step 2: HasData pipeline (SERP + Maps + LinkedIn)                  #
    # ------------------------------------------------------------------ #

    def _run_hasdata(self, company_name: str, url: str) -> Dict:
        """
        Calls DirectoryAndNewsScraper which aggregates SERP, Maps, and LinkedIn.
        Returns a flat dict of the best available fields from all three.
        """
        try:
            dir_data = self._directory.scrape(company_name, url)
            # Flatten: maps fields win for contact; linkedin wins for identity
            merged: Dict = {**dir_data.get("serp", {})}

            maps = dir_data.get("maps", {})
            for contact_field in ("phone", "address", "url"):
                if maps.get(contact_field):
                    merged[contact_field] = maps[contact_field]

            li = dir_data.get("linkedin", {})
            for identity_field in ("name", "introduction", "size", "founded_year",
                                   "industry", "hq_location", "tags", "linkedin_url"):
                if li.get(identity_field) and not merged.get(identity_field):
                    merged[identity_field] = li[identity_field]

            # Store news articles separately so UI can display them
            merged["_news"] = dir_data.get("news", [])
            return merged
        except Exception as e:
            logger.error("HasData pipeline failed: %s", e)
            return {}

    # ------------------------------------------------------------------ #
    #  Step 3: Firecrawl pipeline (website homepage)                      #
    # ------------------------------------------------------------------ #

    # ---- page-keyword scoring for site-map selection -------------------- #

    _TEAM_KW    = {"team", "people", "about", "leadership", "founders", "staff",
                   "crew", "who-we-are", "who_we_are", "management", "executives",
                   "company", "overview", "mission", "story", "history", "culture"}
    _CONTACT_KW = {"contact", "reach", "location", "address", "office", "connect",
                   "inquiry", "support", "help"}
    _PRODUCT_KW = {"product", "solution", "service", "platform", "feature",
                   "technology", "tech", "pricing", "plan"}

    @staticmethod
    def _page_score(page_url: str) -> int:
        path = page_url.lower().rstrip("/")
        slug = path.split("/")[-1]
        # Prioritise high-signal slugs
        if any(kw in slug for kw in CompanyDataScraper._TEAM_KW):
            return 10
        if any(kw in slug for kw in CompanyDataScraper._CONTACT_KW):
            return 9
        if any(kw in slug for kw in CompanyDataScraper._PRODUCT_KW):
            return 7
        # Skip obviously low-value pages
        if any(bad in path for bad in ("/blog/", "/news/", "/press/", "/jobs/",
                                        "/careers/", "/privacy", "/terms",
                                        "/cookie", "/sitemap", "/tag/", "/category/",
                                        "/sessions/", "/newsroom/", "/customers/",
                                        "/support/", "/billing/", "/docs/",
                                        "/developers/", "/changelog/", "/legal/")):
            return -1
        return 0

    @staticmethod
    def _same_origin(base: str, link: str) -> bool:
        try:
            return urlparse(base).netloc == urlparse(link).netloc
        except Exception:
            return False

    def _fetch_sitemap_urls(self, base_url: str) -> List[str]:
        """
        Fetch /sitemap.xml and /sitemap_index.xml, parse <loc> entries.
        Returns a flat list of page URLs on the same domain.
        """
        urls: List[str] = []
        candidates = [
            urljoin(base_url, "/sitemap.xml"),
            urljoin(base_url, "/sitemap_index.xml"),
            urljoin(base_url, "/sitemap/"),
        ]
        for sitemap_url in candidates:
            try:
                result = self._firecrawl.scrape_url(sitemap_url, formats=["html"], only_main_content=False)
                data = result.get("data") or result
                html = data.get("html") or ""
                if not html:
                    continue
                # Parse all <loc> tags from sitemap XML
                try:
                    root = ET.fromstring(html)
                    ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
                    for loc in root.iter("{http://www.sitemaps.org/schemas/sitemap/0.9}loc"):
                        href = (loc.text or "").strip()
                        if href and self._same_origin(base_url, href):
                            urls.append(href)
                except ET.ParseError:
                    # Fall back to regex if not valid XML
                    for m in re.finditer(r'<loc>\s*(https?://[^\s<]+)\s*</loc>', html, re.IGNORECASE):
                        href = m.group(1).strip()
                        if self._same_origin(base_url, href):
                            urls.append(href)
                if urls:
                    logger.info("Sitemap %s yielded %d URLs", sitemap_url, len(urls))
                    break
            except Exception as e:
                logger.debug("Sitemap fetch %s failed: %s", sitemap_url, e)
        return urls

    # ---- Firecrawl AI extraction schema ------------------------------------- #

    _EXTRACT_SCHEMA: Dict = {
        "type": "object",
        "properties": {
            "name":          {"type": "string",  "description": "Company legal or brand name"},
            "introduction":  {"type": "string",  "description": "2-3 sentence company description or mission"},
            "industry":      {"type": "string",  "description": "Primary industry or sector"},
            "size":          {"type": "string",  "description": "Employee count or range, e.g. '50-100 employees'"},
            "founded_year":  {"type": "integer", "description": "Year the company was founded"},
            "hq_location":   {"type": "string",  "description": "City and country of HQ, e.g. 'Seoul, South Korea'"},
            "address":       {"type": "string",  "description": "Full street address"},
            "email":         {"type": "string",  "description": "Primary contact email address"},
            "phone":         {"type": "string",  "description": "Primary phone number"},
            "linkedin_url":  {"type": "string",  "description": "LinkedIn company page URL"},
            "tags":          {"type": "array",   "items": {"type": "string"},
                              "description": "Industry keywords, technology areas, product categories"},
            "pricing_model": {"type": "string",  "description": "Pricing model: SaaS / one-time / freemium / subscription"},
            "revenue_range": {"type": "string",  "description": "Annual revenue range if publicly disclosed"},
            "ceo_name":      {"type": "string",  "description": "CEO or founder name(s)"},
        },
    }

    _EXTRACT_PROMPT = (
        "Extract structured company information from the page content. "
        "Return every field you can find. "
        "Use null for fields not present. "
        "Do not guess or infer — only return information explicitly stated on the page."
    )

    def _run_firecrawl(self, url: str, company_name: str = "", log_cb=None) -> Dict:
        """
        1. Discover all pages via sitemap + map_site.
        2. Scrape all relevant pages for markdown + links.
        3. Run Firecrawl AI schema extraction on the top key pages.
        4. Use regex as fallback for anything AI missed.
        """
        def _log(msg: str) -> None:
            logger.info(msg)
            if log_cb:
                try:
                    log_cb(msg)
                except Exception:
                    pass

        if not self._firecrawl or not url:
            return {}

        # ── Step 1: discover all pages ────────────────────────────────────
        discovered: List[str] = []
        map_url_count = 0

        _log(f"  Checking sitemap for {url}")
        sitemap_urls = self._fetch_sitemap_urls(url)
        discovered.extend(sitemap_urls)
        if sitemap_urls:
            _log(f"  Sitemap: {len(sitemap_urls)} URLs found")

        try:
            _log(f"  Mapping site structure…")
            map_result = self._firecrawl.map_site(url)
            map_urls = map_result.get("links") or []
            map_url_count = len(map_urls)
            existing = set(discovered)
            for u in map_urls:
                if u not in existing and self._same_origin(url, u):
                    discovered.append(u)
            _log(f"  Site map: {map_url_count} URLs found")
        except Exception as e:
            logger.debug("map_site failed for %s: %s", url, e)

        other_pages = [u for u in dict.fromkeys(discovered) if u != url]
        scored = sorted(other_pages, key=self._page_score, reverse=True)
        useful = [u for u in scored if self._page_score(u) >= 0][:10]
        pages_to_scrape = [url] + useful

        _log(f"  Scraping {len(pages_to_scrape)} pages (sitemap: {len(sitemap_urls)}, map: {map_url_count})")

        # ── Step 2: scrape all pages for markdown + html + links ─────────────
        all_parts: List[str] = []
        all_html_parts: List[str] = []
        all_links: List[str] = []
        home_md = ""
        page_failures: List[Dict] = []

        for i, page_url in enumerate(pages_to_scrape, 1):
            slug = page_url.replace(url, "").strip("/") or "(homepage)"
            try:
                result = self._firecrawl.scrape_url(
                    page_url, formats=["markdown", "html", "links"], only_main_content=False
                )
                data  = result.get("data") or result
                md    = data.get("markdown") or ""
                html  = data.get("html") or ""
                links = data.get("links") or []
                if md:
                    all_parts.append(md)
                    if not home_md:
                        home_md = md
                    _log(f"  [{i}/{len(pages_to_scrape)}] OK   /{slug} ({len(md)} chars)")
                else:
                    _log(f"  [{i}/{len(pages_to_scrape)}] SKIP /{slug} (empty)")
                if html:
                    all_html_parts.append(html)
                all_links.extend(links)
            except Exception as e:
                logger.debug("Firecrawl page failed %s: %s", page_url, e)
                page_failures.append({"url": page_url, "error": str(e)})
                _log(f"  [{i}/{len(pages_to_scrape)}] ERR  /{slug} — {e}")

        page_results = {
            "attempted":    len(pages_to_scrape),
            "succeeded":    len(all_parts),
            "sitemap_urls": len(sitemap_urls),
            "map_urls":     map_url_count,
            "failures":     page_failures,
        }

        if not all_parts:
            _log(f"  No markdown scraped for {url}")
            return {"url": url, "_page_results": page_results}

        combined      = "\n\n".join(all_parts)
        combined_html = "\n\n".join(all_html_parts)
        out: Dict = {"url": url, "_combined_markdown": combined, "_page_results": page_results}

        # ── Step 3: AI schema extraction on key pages ─────────────────────
        # Run Firecrawl's built-in AI extraction on homepage + top 4 priority pages.
        # This replaces fragile regex for most fields.
        ai_pages = [url] + [p for p in useful[:4] if self._page_score(p) >= 7]
        _log(f"  Running AI extraction on {len(ai_pages)} key pages…")
        merged_ai: Dict = {}
        try:
            extracts = self._firecrawl.batch_extract(
                ai_pages, self._EXTRACT_SCHEMA, self._EXTRACT_PROMPT
            )
            for ex in extracts:
                for k, v in (ex or {}).items():
                    if v not in (None, "", [], {}) and k not in merged_ai:
                        merged_ai[k] = v
            if merged_ai:
                _log(f"  AI extracted {len(merged_ai)} fields: {', '.join(merged_ai.keys())}")
            else:
                _log("  AI extraction returned no fields — falling back to regex")
        except Exception as e:
            _log(f"  AI extraction failed — using regex only: {e}")

        # Apply AI results to out (they take priority over regex)
        for k, v in merged_ai.items():
            if v not in (None, "", [], {}):
                if k == "ceo_name":
                    out["_ceo_names"] = [n.strip() for n in str(v).split(",") if n.strip()]
                else:
                    out[k] = v

        # ── Step 4: regex fallback for fields AI missed ───────────────────

        # Name — H1 from homepage
        if not out.get("name"):
            h1_m = re.search(r'^#\s+(.+)$', home_md, re.MULTILINE)
            if h1_m:
                h1_text = h1_m.group(1).strip()
                if len(h1_text.split()) <= 6 and len(h1_text) <= 60:
                    out["name"] = h1_text

        # Introduction — first long prose paragraph
        if not out.get("introduction"):
            for line in combined.split("\n"):
                ln = line.strip()
                if len(ln) < 80:
                    continue
                if ln.startswith(("#", "!", "-", "*", "|")):
                    continue
                if ln.count("](") / max(len(ln), 1) > 0.02:
                    continue
                if ln.count("\\") > 3:
                    continue
                out["introduction"] = ln[:800]
                break

        # Email
        if not out.get("email"):
            label_email = re.search(
                r'(?:e-?mail|contact|email)[.\s:]+([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})',
                combined, re.IGNORECASE
            )
            if label_email:
                out["email"] = label_email.group(1)
            else:
                mailto_emails = [lk.replace("mailto:", "").split("?")[0].strip()
                                 for lk in all_links if lk.startswith("mailto:")]
                if not mailto_emails:
                    raw_emails = re.findall(r'\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b', combined)
                    mailto_emails = [e for e in raw_emails
                                     if not re.search(r'\.(png|jpg|gif|css|js|svg|ico)$', e, re.IGNORECASE)]
                if mailto_emails:
                    out["email"] = mailto_emails[0]

        # LinkedIn
        if not out.get("linkedin_url"):
            li_links = [lk for lk in all_links if re.search(r'linkedin\.com/company/', lk, re.IGNORECASE)]
            if li_links:
                out["linkedin_url"] = li_links[0].split("?")[0]
            else:
                li_m = re.search(r'https?://(?:www\.)?linkedin\.com/company/[A-Za-z0-9\-_%]+', combined)
                if li_m:
                    out["linkedin_url"] = li_m.group(0).rstrip("/")

        # Phone
        if not out.get("phone"):
            label_phone = re.search(
                r'(?:tel|phone|ph|fax)[.\s:]+(\+?[\d][\d\s\-().]{4,18}[\d])',
                combined, re.IGNORECASE
            )
            if label_phone:
                digits = re.sub(r'\D', '', label_phone.group(1))
                if 6 <= len(digits) <= 15:
                    out["phone"] = label_phone.group(1).strip()
            else:
                for phone_m in re.finditer(r'(\+?[\d][\d\s\-().]{6,18}[\d])', combined):
                    digits = re.sub(r'\D', '', phone_m.group(1))
                    if 7 <= len(digits) <= 15:
                        out["phone"] = phone_m.group(1).strip()
                        break

        # Address
        if not out.get("address"):
            label_addr = re.search(r'Address[.\s:]+([^\n]{10,120})', combined, re.IGNORECASE)
            if label_addr:
                out["address"] = label_addr.group(1).strip()
            else:
                addr_m = re.search(
                    r'\d{1,6}[-\w\s,]+'
                    r'(?:Street|St\.?|Avenue|Ave\.?|Road|Rd\.?|Boulevard|Blvd|Drive|Dr\.?'
                    r'|Lane|Ln|Way|Court|Ct|Floor|F|Suite|Ste|Building|Bldg|gu|dong|ro)\b'
                    r'[^\n]{0,80}',
                    combined, re.IGNORECASE
                )
                if addr_m:
                    out["address"] = addr_m.group(0).strip()

        # HQ location
        if not out.get("hq_location"):
            hq_m = re.search(
                r'(?:headquartered|located|based|offices?)\s+in\s+([A-Za-z][A-Za-z\s,]{3,60}?)(?:\.|,|\n|$)',
                combined, re.IGNORECASE
            )
            if hq_m:
                out["hq_location"] = hq_m.group(1).strip().rstrip(",")
            elif out.get("address"):
                addr_parts = [p.strip() for p in out["address"].split(",")]
                if len(addr_parts) >= 2:
                    out["hq_location"] = addr_parts[-1]

        # Founded year
        if not out.get("founded_year"):
            founded_m = re.search(
                r'(?:founded|established|incorporated|est\.)[^\d]{0,12}(\d{4})'
                r'|(\d{4})[.\-\s]{0,8}(?:\d{2}[.\-\s]{0,4})?(?:Established|Founded|Incorporated)',
                combined, re.IGNORECASE
            )
            if founded_m:
                year = int(founded_m.group(1) or founded_m.group(2))
                if 1900 <= year <= 2030:
                    out["founded_year"] = year

        # CEO from footer (regex only — if AI didn't find it)
        if not out.get("_ceo_names"):
            ceo_m = re.search(r'CEO[.\s:]+([A-Za-z][A-Za-z\s,&]{2,60}?)(?:\n|$)', combined, re.IGNORECASE)
            if ceo_m:
                raw_ceo = ceo_m.group(1).strip().rstrip(",")
                out["_ceo_names"] = [n.strip() for n in raw_ceo.split(",") if n.strip()]

        # Industry keyword scan fallback
        if not out.get("industry"):
            out["industry"] = self._detect_industry(combined)

        # Tags
        if not out.get("tags"):
            out["tags"] = self._extract_tags(combined)

        # Size
        if not out.get("size"):
            size_m = re.search(
                r'(\d[\d,]*)\s*\+?\s*(?:employees|full[- ]?time|team members|professionals|staff|people)\b'
                r'|(?:team of|over|more than|around)\s+(\d[\d,]*)\s+(?:people|employees|professionals)',
                combined, re.IGNORECASE
            )
            if size_m:
                num = (size_m.group(1) or size_m.group(2) or "").replace(",", "")
                out["size"] = f"{num} employees"

        # ── Step 5: HTML extraction (structured elements + free-text label:value) ─
        if combined_html:
            tl = self._extract_text_labels(combined_html)
            el = self._extract_element_labels(combined_html)
            # Element-based wins over text-based (more precise)
            html_data = {**tl, **el}

            for field, labels in [
                ("address",      ["address"]),
                ("phone",        ["phone"]),
                ("email",        ["email"]),
                ("founded_year", ["founded_year"]),
                ("size",         ["size"]),
                ("hq_location",  ["hq_location"]),
                ("ceo_name",     ["ceo_name"]),
            ]:
                if not out.get(field) and html_data.get(field):
                    val = html_data[field]
                    if field == "email":
                        em = re.search(r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}', val)
                        val = em.group(0) if em else ""
                    elif field == "phone":
                        digits = re.sub(r'\D', '', val)
                        if not (7 <= len(digits) <= 15):
                            val = ""
                    elif field == "founded_year":
                        yr = re.search(r'(19|20)\d{2}', val)
                        val = int(yr.group(0)) if yr else None
                    if val:
                        out[field] = val
                        _log(f"  HTML found {field}: {val}")

            # CEO name → seed team list if nothing found yet
            if not out.get("_ceo_names") and html_data.get("ceo_name"):
                out["_ceo_names"] = [n.strip() for n in html_data["ceo_name"].split(",") if n.strip()]

            # Derive hq_location from address if still missing
            if not out.get("hq_location") and out.get("address"):
                parts = [p.strip() for p in out["address"].split(",")]
                if len(parts) >= 2:
                    out["hq_location"] = parts[-1]

        return out

    # ---- Industry keyword scanner ---------------------------------------- #

    # Map of regex patterns → canonical industry name (order matters: more specific first)
    _INDUSTRY_MAP = [
        (r"surgical robot|robotic surgery|endoscop|laparoscop|minimally.invasive surgery", "Surgical Robotics"),
        (r"medical robot|surgical system|operating room robot", "Medical Robotics"),
        (r"medical device|medtech|med-tech|health device|diagnostic device", "Medical Devices"),
        (r"digital health|healthtech|health-tech|telehealth|telemedicine|patient care software", "Digital Health"),
        (r"biotech|biotechnology|genomic|drug discovery|life science|pharmaceutical", "Biotechnology"),
        (r"artificial intelligence|machine learning|deep learning|neural network|large language model", "Artificial Intelligence"),
        (r"cybersecurity|cyber security|infosec|network security|endpoint security|zero.trust", "Cybersecurity"),
        (r"fintech|financial technology|payment processing|neobank|insurtech|wealthtech", "Financial Technology"),
        (r"edtech|education technology|e.learning|online learning|lms|learning management", "Education Technology"),
        (r"logistics|supply chain|warehouse|freight|last.mile|fleet management", "Logistics & Supply Chain"),
        (r"e.commerce|ecommerce|online retail|marketplace|direct.to.consumer", "E-commerce"),
        (r"real estate|proptech|property technology|smart building|facility management", "Real Estate Technology"),
        (r"clean energy|renewable energy|solar|wind energy|energy storage|climatetech", "Clean Energy"),
        (r"agtech|agri.tech|precision agriculture|crop monitoring|smart farming", "AgriTech"),
        (r"saas|software as a service|cloud platform|cloud software|b2b software", "SaaS"),
        (r"autonomous vehicle|self.driving|robotaxi|lidar|autonomous driving", "Autonomous Vehicles"),
        (r"drone|uav|unmanned aerial|unmanned vehicle", "Drones & UAV"),
        (r"robotics|automation|industrial robot|cobot|collaborative robot", "Robotics & Automation"),
        (r"manufacturing|factory|industrial|cnc|precision engineering", "Manufacturing"),
        (r"gaming|video game|game engine|esport|game development", "Gaming"),
        (r"hr.?tech|human resources|talent management|recruiting software|payroll", "HR Technology"),
        (r"legal.?tech|contract management|law firm software|legal ai", "Legal Technology"),
        (r"marketing.?tech|martech|advertising technology|programmatic|ad.?tech", "Marketing Technology"),
        (r"iot|internet of things|smart sensor|connected device|edge computing", "IoT"),
        (r"blockchain|web3|decentralized|crypto|nft|defi|smart contract", "Blockchain & Web3"),
        (r"space|satellite|aerospace|rocket|launch vehicle", "Aerospace & Space"),
        (r"defense|military|security system|surveillance|national security", "Defense & Security"),
    ]

    @classmethod
    def _detect_industry(cls, text: str) -> str:
        text_lower = text.lower()
        for pattern, industry in cls._INDUSTRY_MAP:
            if re.search(pattern, text_lower):
                return industry
        return ""

    # ---- Tags extractor -------------------------------------------------- #

    # Well-known technology / product keywords that make good tags
    _TAG_KEYWORDS = [
        "AI", "machine learning", "deep learning", "computer vision", "NLP",
        "robotics", "automation", "IoT", "cloud", "SaaS", "API", "open source",
        "blockchain", "web3", "mobile", "real-time", "analytics", "data platform",
        "surgery", "medical", "healthcare", "clinical", "endoscopy", "minimally invasive",
        "autonomous", "drone", "electric vehicle", "battery", "solar",
        "fintech", "payments", "lending", "insurance", "wealth management",
        "marketplace", "e-commerce", "logistics", "last mile", "supply chain",
        "cybersecurity", "zero trust", "encryption", "authentication",
        "edtech", "LMS", "online learning", "virtual reality", "AR", "VR",
    ]

    @classmethod
    def _extract_tags(cls, text: str) -> List[str]:
        found = []
        # 1. Check explicit label sections first
        label_m = re.search(
            r'(?:tags?|keywords?|specialt(?:y|ies)|categories)[:\s]+([A-Za-z0-9,\s|·•\-]{5,300})',
            text, re.IGNORECASE
        )
        if label_m:
            raw = re.split(r'[,|\n·•]', label_m.group(1))
            found = [t.strip() for t in raw if 2 < len(t.strip()) < 40][:10]

        # 2. Scan for known keyword phrases
        if not found:
            text_lower = text.lower()
            for kw in cls._TAG_KEYWORDS:
                if kw.lower() in text_lower:
                    found.append(kw)
                if len(found) >= 8:
                    break

        # 3. Pull H2/H3 headings that are short and look like product/feature names
        if not found:
            headings = re.findall(r'^#{2,3}\s+(.+)$', text, re.MULTILINE)
            found = [h.strip() for h in headings if 3 < len(h.strip()) < 35][:6]

        return found

    # ------------------------------------------------------------------ #
    #  Step 4: Apify pipeline (team search)                               #
    # ------------------------------------------------------------------ #

    # ---- HTML label extraction ------------------------------------------- #

    # Label → canonical field name mapping (regex pattern → field)
    _LABEL_FIELD_MAP = [
        (r"e-?mail",                    "email"),
        (r"tel(?:ephone)?|phone|전화",   "phone"),
        (r"fax|팩스",                    "fax"),
        (r"address|주소|사무실",          "address"),
        (r"ceo|founder|대표|representative", "ceo_name"),
        (r"founded|established|설립",   "founded_year"),
        (r"employees?|headcount|인원",  "size"),
        (r"hq|headquarters|본사",       "hq_location"),
        (r"registration|business.?no|사업자", "registration"),
    ]

    _STREET_HINT_RE = re.compile(
        r'(?:\d[\w\-,\s]*(?:로|길|ro|street|st\.?|avenue|ave|road|rd|blvd|floor|f\b|'
        r'suite|ste|building|bldg|gu|dong|구|동))',
        re.IGNORECASE,
    )

    @classmethod
    def _clean_html_text(cls, html: str) -> str:
        """Convert HTML to clean plain text, preserving line breaks."""
        text = re.sub(r'</(?:div|p|li|section|article|tr|h[1-6])\b[^>]*>', '\n', html, flags=re.IGNORECASE)
        text = re.sub(r'<br\s*/?>', '\n', text, flags=re.IGNORECASE)
        text = re.sub(r'<[^>]+>', ' ', text)
        text = text.replace('&amp;', '&').replace('&nbsp;', ' ').replace('&lt;', '<').replace('&gt;', '>').replace('&#39;', "'")
        text = re.sub(r'[ \t]+', ' ', text)
        return text

    @classmethod
    def _extract_text_labels(cls, html: str) -> Dict[str, str]:
        """
        Strip HTML and extract Label : value pairs from free-text info blocks.
        Handles patterns like:
          <div>Tel : +82-70-4754-9619</div>
          <div>Registration : 123 | CEO : Name <br>Address line</div>
          <div>E-mail : info@company.com</div>
        """
        text = cls._clean_html_text(html)
        result: Dict[str, str] = {}
        orphan_lines: List[str] = []

        for raw_line in text.split('\n'):
            segments = [s.strip() for s in raw_line.split('|') if s.strip()]
            for seg in segments:
                m = re.match(r'^([A-Za-z가-힣\s\-\.]{2,30}?)\s*[：:]\s*(.+)$', seg)
                if m:
                    label_raw = m.group(1).strip()
                    value     = m.group(2).strip()
                    if not value:
                        continue
                    for pattern, field in cls._LABEL_FIELD_MAP:
                        if re.search(pattern, label_raw, re.IGNORECASE):
                            if field not in result:
                                result[field] = value
                            break
                    else:
                        orphan_lines.append(seg)
                elif len(seg) > 15:
                    orphan_lines.append(seg)

        # Recover address from orphan lines that look like street addresses
        if "address" not in result:
            for line in orphan_lines:
                if cls._STREET_HINT_RE.search(line) and len(line) > 15:
                    result["address"] = line.strip()
                    break

        return result

    @classmethod
    def _extract_element_labels(cls, html: str) -> Dict[str, str]:
        """
        Extract label:value from structured HTML elements:
          <hN>Label</hN><p>value</p>
          <dt>Label</dt><dd>value</dd>
          <td>Label</td><td>value</td>
          <span/strong>Label</span>value
        """
        result: Dict[str, str] = {}

        def _try(labels: List[str]) -> str:
            label_re = "|".join(re.escape(l) for l in labels)
            patterns = [
                rf'<h[1-6][^>]*>\s*(?:{label_re})[^<]*</h[1-6]>\s*(?:<[^/][^>]*>)?\s*([^<]{{3,300}})',
                rf'<dt[^>]*>\s*(?:{label_re})[^<]*</dt>\s*<dd[^>]*>\s*([^<]{{3,300}})',
                rf'<td[^>]*>\s*(?:{label_re})[^<]*</td>\s*<td[^>]*>\s*([^<]{{3,300}})',
                rf'<(?:span|strong|b|em|label)[^>]*>\s*(?:{label_re})[^<]*</(?:span|strong|b|em|label)>\s*([^<]{{3,300}})',
            ]
            for pat in patterns:
                m = re.search(pat, html, re.IGNORECASE | re.DOTALL)
                if m:
                    val = re.sub(r'\s+', ' ', m.group(1)).strip()
                    val = val.replace('&amp;', '&').replace('&nbsp;', ' ')
                    if len(val) >= 3:
                        return val
            return ""

        checks = [
            ("address",      ["Address", "주소", "사무실 주소"]),
            ("phone",        ["Tel", "TEL", "Phone", "전화", "전화번호"]),
            ("email",        ["E-mail", "Email", "이메일", "Mail"]),
            ("founded_year", ["Founded", "Established", "설립"]),
            ("size",         ["Employees", "Team size", "인원"]),
            ("hq_location",  ["HQ", "Headquarters", "본사"]),
            ("ceo_name",     ["CEO", "Founder", "대표이사"]),
        ]
        for field, labels in checks:
            v = _try(labels)
            if v:
                result[field] = v

        return result

    def _run_apify(self, company_name: str, url: str) -> Dict:
        """Run LinkedIn team search via Apify."""
        if not self._apify:
            return {}
        try:
            team = self._teams.extract(company_name, url)
            return {"team_members": team} if team else {}
        except Exception as e:
            logger.error("Apify pipeline failed: %s", e)
            return {}

    # ------------------------------------------------------------------ #
    #  Public entry point                                                  #
    # ------------------------------------------------------------------ #

    def scrape(
        self,
        company_name: str,
        url: str = "",
        log_cb: Optional[Any] = None,
    ) -> CompanyProfile:
        """
        Firecrawl-only pipeline. Returns a CompanyProfile with accuracy metadata.
        HasData and Apify are not used — all data comes from Firecrawl.

        log_cb: optional callable(str) — called with progress messages for live UI display.
        """
        def _log(msg: str) -> None:
            logger.info(msg)
            if log_cb:
                try:
                    log_cb(msg)
                except Exception:
                    pass

        _log(f"Starting scrape: {company_name} — {url}")

        canonical_url = url.strip() if url else ""
        if not canonical_url:
            _log("WARNING: No URL provided — Firecrawl will have nothing to scrape")

        sources: Dict[str, Dict] = {}
        source_errors: Dict[str, str] = {}
        news_data: List[Dict] = []

        # ── Step 1: Firecrawl + Directory sources in parallel ─────────────
        # Firecrawl crawls the website; DirectoryAndNewsScraper runs HasData
        # SERP, HasData Maps, and LinkedIn Company (Apify) simultaneously.
        _log(f"[1/5] Website crawl + SERP / Maps / LinkedIn — running in parallel…")

        def _run_dir() -> Dict:
            try:
                return self._directory.scrape(company_name, canonical_url)
            except Exception as _de:
                logger.error("Directory sources failed for %r: %s", company_name, _de)
                return {}

        with ThreadPoolExecutor(max_workers=2) as _exe:
            _fc_fut  = _exe.submit(self._run_firecrawl, canonical_url, company_name, _log)
            _dir_fut = _exe.submit(_run_dir)

            try:
                sources["firecrawl"] = _fc_fut.result()
            except Exception as e:
                _log(f"  ERROR: Firecrawl failed — {e}")
                sources["firecrawl"] = {}
                source_errors["firecrawl"] = str(e)

            dir_result = _dir_fut.result()

        # Log Firecrawl summary
        pr = sources.get("firecrawl", {}).get("_page_results", {})
        _log(f"  Firecrawl → {pr.get('succeeded', 0)}/{pr.get('attempted', 0)} pages scraped"
             f"  (sitemap: {pr.get('sitemap_urls', 0)}, map: {pr.get('map_urls', 0)})")
        if pr.get("failures"):
            for fail in pr["failures"]:
                _log(f"  ✗ {fail['url']} — {fail['error']}")
        n_fields = len([k for k in sources.get("firecrawl", {})
                        if not k.startswith("_") and sources["firecrawl"].get(k)])
        _log(f"  Firecrawl → {n_fields} fields extracted")

        # Unpack directory results into separate weighted sources
        serp_data     = dir_result.get("serp")     or {}
        maps_data     = dir_result.get("maps")     or {}
        linkedin_data = dir_result.get("linkedin") or {}
        news_data     = dir_result.get("news")     or []

        if serp_data:
            sources["serp"] = serp_data
            _log(f"  SERP → {sum(1 for v in serp_data.values() if v)} fields"
                 f"  (knowledge panel)")
        if maps_data:
            sources["maps"] = maps_data
            _log(f"  Maps → {sum(1 for v in maps_data.values() if v)} fields"
                 f"  (verified contact)")
        if linkedin_data:
            sources["linkedin"] = linkedin_data
            _log(f"  LinkedIn Co. → {sum(1 for v in linkedin_data.values() if v)} fields")
        if news_data:
            _log(f"  News → {len(news_data)} articles")

        # --- Seed merger with caller-supplied values as lowest-priority fallback ---
        provided = {k: v for k, v in {"name": company_name, "url": canonical_url}.items() if v}
        if provided:
            sources["provided"] = provided

        # --- Pop internal keys before merging ---
        fc = sources.get("firecrawl", {})
        ceo_names    = fc.pop("_ceo_names", None) or []
        page_results = fc.pop("_page_results", {})
        fc.pop("_combined_markdown", None)

        # --- Step 2: Team members from website (Firecrawl) ---
        _log("[2/5] Extracting team members from website")
        try:
            raw_team = self._teams.extract_from_website(canonical_url, log_cb=_log) if canonical_url else []
            team_dicts = [m.to_dict() for m in raw_team]
            if not team_dicts and ceo_names:
                team_dicts = [{"name": n, "role": "CEO", "email": "", "linkedin_url": "",
                               "source": "website", "confidence": "medium", "issues": []}
                              for n in ceo_names]
            if team_dicts:
                sources["firecrawl"]["team_members"] = team_dicts
                _log(f"  → {len(team_dicts)} team members found")
            else:
                _log("  → No team members found")
        except Exception as e:
            _log(f"  ERROR: {e}")

        # --- Step 3: Products (Firecrawl) ---
        _log("[3/5] Extracting products")
        try:
            products = self._products.extract(canonical_url) if canonical_url else []
            if products:
                sources["firecrawl"]["products"] = products
                for p in products:
                    if p.get("pricing_model") and not sources["firecrawl"].get("pricing_model"):
                        sources["firecrawl"]["pricing_model"] = p["pricing_model"]
                _log(f"  → {len(products)} products found")
            else:
                _log("  → No products found")
        except Exception as e:
            _log(f"  ERROR: {e}")

        # --- Step 4: Funding via Crunchbase (Apify pratikdani → Firecrawl fallback) ---
        _log("[4/5] Checking Crunchbase for funding data")
        try:
            # Build known_signals for slug disambiguation — website_url is the strongest signal
            _cb_signals: Dict = {"company_name": company_name}
            if canonical_url:
                _cb_signals["website_url"] = canonical_url
            fc_data = sources.get("firecrawl") or {}
            if fc_data.get("founded_year"):
                _cb_signals["founded_year"] = fc_data["founded_year"]
            if fc_data.get("hq_location"):
                _cb_signals["hq_country"] = fc_data["hq_location"].split(",")[-1].strip()
            if fc_data.get("introduction"):
                _cb_signals["description"] = fc_data["introduction"][:200]
            cb = self._funding.fetch_crunchbase_data(company_name, known_signals=_cb_signals)
            funding_dicts = [r.to_dict() for r in cb.rounds]
            if funding_dicts:
                sources["firecrawl"]["funding_rounds"] = funding_dicts
            if cb.total_funding_usd:
                sources["firecrawl"]["total_funding"] = cb.total_funding_usd
            elif funding_dicts:
                total = sum(r.get("amount_usd") or 0 for r in funding_dicts)
                if total:
                    sources["firecrawl"]["total_funding"] = total
            if cb.investors:
                sources["firecrawl"]["investors"] = cb.investors
            elif funding_dicts:
                leads = list({inv for r in funding_dicts for inv in (r.get("lead_investors") or []) if inv})
                if leads:
                    sources["firecrawl"]["investors"] = leads
            if cb.revenue_range and not sources["firecrawl"].get("revenue_range"):
                sources["firecrawl"]["revenue_range"] = cb.revenue_range
            if funding_dicts:
                _log(f"  → {len(funding_dicts)} rounds | total ${cb.total_funding_usd:,}" if cb.total_funding_usd
                     else f"  → {len(funding_dicts)} rounds found")
            else:
                _log("  → No funding data found on Crunchbase")
        except Exception as e:
            _log(f"  ERROR: {e}")

        # --- Step 7: Merge ---
        merge_result = self._merger.merge(sources)

        # --- Step 8: Validate ---
        validation = self._validator.validate_company(
            merge_result.merged, merge_result.source_map
        )

        # --- Step 9: Build profile ---
        m = merge_result.merged

        # Logo: prefer scraped logo_url; fall back to Clearbit (free, frontend handles 404)
        _logo = m.get("logo_url") or ""
        if not _logo and canonical_url:
            _domain = urlparse(canonical_url).netloc.replace("www.", "")
            if _domain:
                _logo = f"https://logo.clearbit.com/{_domain}"

        # Rating may come as float or string
        _rating = m.get("rating")
        try:
            _rating = float(_rating) if _rating is not None else None
        except (TypeError, ValueError):
            _rating = None

        _review_count = m.get("review_count")
        try:
            _review_count = int(_review_count) if _review_count is not None else None
        except (TypeError, ValueError):
            _review_count = None

        profile = CompanyProfile(
            name          = m.get("name") or company_name,
            tags          = m.get("tags") or [],
            introduction  = m.get("introduction") or "",
            industry      = m.get("industry") or "",
            size          = m.get("size") or "",
            founded_year  = m.get("founded_year"),
            hq_location   = m.get("hq_location") or "",
            address       = m.get("address") or "",
            linkedin_url  = m.get("linkedin_url") or "",
            email         = m.get("email") or "",
            phone         = m.get("phone") or "",
            url           = m.get("url") or canonical_url,
            logo_url      = _logo,
            tagline       = m.get("tagline") or "",
            products      = m.get("products") or [],
            pricing_model = m.get("pricing_model") or "",
            revenue_range = m.get("revenue_range") or "",
            rating        = _rating,
            review_count  = _review_count,
            team_members  = m.get("team_members") or [],
            funding_rounds = m.get("funding_rounds") or [],
            total_funding = m.get("total_funding"),
            investors     = m.get("investors") or [],
            news          = news_data,
            _validation   = validation.to_dict(),
            _merge_report = merge_result.to_dict(),
            _source_map   = merge_result.source_map,
            _sources_used = list(sources.keys()),
        )

        conf_pct = round((validation.overall_confidence or 0) * 100)
        missing  = validation.missing_critical
        _log(f"Done — {conf_pct}% confidence | missing: {', '.join(missing) if missing else 'none'}")

        # --- Write error/debug log ---
        write_scrape_log(
            company_name=company_name,
            url=canonical_url or url,
            source_results=sources,
            source_errors=source_errors,
            page_results=page_results,
            missing_critical=validation.missing_critical,
            overall_confidence=validation.overall_confidence,
            conflicts=merge_result.conflicts,
        )

        # --- Update learner with results ---
        if canonical_url:
            pages_failed = [p["url"] for p in page_results.get("failures", [])]
            fields_found   = [f for f in merge_result.merged if merge_result.merged.get(f)
                              and not f.startswith("_")]
            fields_missing = validation.missing_critical
            _learner.record_result(
                url=canonical_url,
                pages_ok=[],
                pages_failed=pages_failed,
                fields_found=fields_found,
                fields_missing=fields_missing,
            )

        return profile
