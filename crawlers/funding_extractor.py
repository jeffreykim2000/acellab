"""FundingExtractor — collects funding history from HasData, Crunchbase, and news."""
import re
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

ROUND_RE = re.compile(
    r"\b(pre-?seed|seed|series\s+[A-F]|angel|bridge|ipo|acquisition|"
    r"grant|debt\s+financing|convertible\s+note|crowdfunding|venture)\b",
    re.IGNORECASE,
)
AMOUNT_RE = re.compile(
    r"\$\s?([\d,.]+)\s?(million|billion|thousand|M|B|K)\b",
    re.IGNORECASE,
)
DATE_RE = re.compile(
    r"\b(\d{4}[-/]\d{1,2}[-/]\d{1,2}|(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},?\s+\d{4})\b"
)

MULTIPLIERS = {
    "k": 1_000, "thousand": 1_000,
    "m": 1_000_000, "million": 1_000_000,
    "b": 1_000_000_000, "billion": 1_000_000_000,
}

# Directories/news sources good for funding data
FUNDING_SOURCES = [
    "techcrunch.com", "crunchbase.com", "reuters.com",
    "bloomberg.com", "businesswire.com", "prnewswire.com",
]


@dataclass
class _CrunchbaseData:
    rounds:            List["FundingRound"] = field(default_factory=list)
    total_funding_usd: Optional[int]        = None
    investors:         List[str]            = field(default_factory=list)
    revenue_range:     str                  = ""


class FundingRound:
    def __init__(
        self,
        round_type: str = "",
        amount_usd: Optional[int] = None,
        announced_date: str = "",
        lead_investors: Optional[List[str]] = None,
        source: str = "",
        confidence: str = "medium",
    ):
        self.round_type    = round_type
        self.amount_usd    = amount_usd
        self.announced_date = announced_date
        self.lead_investors = lead_investors or []
        self.source        = source
        self.confidence    = confidence
        self.issues: List[str] = []

    def to_dict(self) -> dict:
        return {
            "round_type":      self.round_type,
            "amount_usd":      self.amount_usd,
            "announced_date":  self.announced_date,
            "lead_investors":  self.lead_investors,
            "source":          self.source,
            "confidence":      self.confidence,
            "issues":          self.issues,
        }


class FundingExtractor:
    """
    Funding pipeline (in priority order):
    1. Crunchbase via Apify — structured data, highest confidence
    2. HasData Google SERP — funding news snippets, good for recent rounds
    Deduplicates by round_type + announced_date and returns sorted history.
    """

    def __init__(self, hasdata_client=None, firecrawl_client=None, apify_client=None):
        self._hasdata  = hasdata_client
        self._firecrawl = firecrawl_client
        self._apify    = apify_client

    # ------------------------------------------------------------------ #
    #  Text parsing helpers                                                #
    # ------------------------------------------------------------------ #

    def _parse_amount(self, text: str) -> Optional[int]:
        """Convert "$5M" or "5 million" to an integer dollar amount."""
        m = AMOUNT_RE.search(text)
        if not m:
            return None
        try:
            base = float(m.group(1).replace(",", ""))
            mult = MULTIPLIERS.get(m.group(2).lower().strip(), 1)
            return int(base * mult)
        except (ValueError, AttributeError):
            return None

    def _parse_date(self, text: str) -> str:
        """Extract and normalise a date string to YYYY-MM-DD."""
        m = DATE_RE.search(text)
        if not m:
            return ""
        raw = m.group(1)
        for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%B %d, %Y", "%B %d %Y",
                    "%b %d, %Y", "%b. %d, %Y"):
            try:
                return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
            except ValueError:
                continue
        return raw

    def _extract_investors(self, text: str) -> List[str]:
        """Extract investor names from sentences like 'led by X and Y'."""
        patterns = [
            r"led\s+by\s+([\w\s,&.]+?)(?:\.|and participated|\Z)",
            r"investors?\s+(?:include|:)\s*([\w\s,&.]+?)(?:\.|led|\Z)",
            r"backed\s+by\s+([\w\s,&.]+?)(?:\.|and|\Z)",
        ]
        names: List[str] = []
        for pat in patterns:
            for m in re.finditer(pat, text, re.IGNORECASE):
                raw = m.group(1).strip()
                for part in re.split(r",\s*|\s+and\s+", raw):
                    part = part.strip().strip(".")
                    if 2 < len(part) < 60 and part not in names:
                        names.append(part)
        return names

    def _parse_funding_from_text(self, text: str, source: str) -> List[FundingRound]:
        """Parse funding rounds from free-form text (SERP snippets, article bodies)."""
        rounds: List[FundingRound] = []
        for sentence in re.split(r"(?<=[.!?])\s+", text):
            round_m = ROUND_RE.search(sentence)
            amount  = self._parse_amount(sentence)
            if not round_m and not amount:
                continue

            r = FundingRound(
                round_type     = round_m.group(0).title() if round_m else "Unknown",
                amount_usd     = amount,
                announced_date = self._parse_date(sentence),
                lead_investors = self._extract_investors(sentence),
                source         = source,
                confidence     = "high" if (amount and self._parse_date(sentence)) else
                                 "medium" if amount else "low",
            )
            if not r.announced_date:
                r.issues.append("No date found")
            if not r.amount_usd:
                r.issues.append("No amount found")
            rounds.append(r)

        return rounds

    # ------------------------------------------------------------------ #
    #  Source: HasData SERP                                                #
    # ------------------------------------------------------------------ #

    def fetch_from_serp(self, company_name: str) -> List[FundingRound]:
        """Search Google via HasData for funding announcements."""
        if not self._hasdata:
            return []
        try:
            result = self._hasdata.serp_search(
                f"{company_name} funding round raised investment", num_results=10
            )
            rounds: List[FundingRound] = []
            for item in result.get("organic_results", []):
                text = (item.get("snippet") or "") + " " + (item.get("title") or "")
                rounds.extend(self._parse_funding_from_text(text, "serp"))
            return rounds
        except Exception as e:
            logger.error("SERP funding failed for %r: %s", company_name, e)
            return []

    # ------------------------------------------------------------------ #
    #  Source: Crunchbase via Apify (pratikdani bulk actor)               #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _cb_slug(company_name: str) -> str:
        return re.sub(r"[^a-z0-9]+", "-", company_name.lower()).strip("-")

    def _parse_pratikdani(self, d: dict) -> "CrunchbaseData":
        """Parse pratikdani actor output (Crunchbase internal page-data format)."""
        # Funding summary
        fund_sum = d.get("funding_rounds_summary") or {}
        total_usd = (fund_sum.get("funding_total") or {}).get("value_usd") or \
                    (d.get("frequently_asked_questions_total_funding") or {}).get(
                        "funding_total", {}).get("value_usd")

        # Funding rounds
        rounds: List[FundingRound] = []
        for r in (d.get("funding_rounds_list") or []):
            if not isinstance(r, dict):
                continue
            amt_usd = (r.get("money_raised") or {}).get("value_usd") or r.get("raised_amount_usd")
            lead_raw = r.get("lead_investor_identifiers") or []
            lead_names = [li.get("value") for li in lead_raw
                          if isinstance(li, dict) and li.get("value")]
            fr = FundingRound(
                round_type     = r.get("investment_type") or "",
                amount_usd     = int(amt_usd) if amt_usd else None,
                announced_date = r.get("announced_on") or "",
                lead_investors = lead_names,
                source         = "crunchbase_apify",
                confidence     = "high",
            )
            if not fr.amount_usd:
                fr.issues.append("Amount not disclosed")
            rounds.append(fr)

        # All investors (not just leads)
        inv_names: List[str] = []
        for inv in (d.get("investors_list") or []):
            if not isinstance(inv, dict):
                continue
            # investor_identifier.value = firm name; identifier.value = investment record name
            n = (inv.get("investor_identifier") or {}).get("value") or \
                (inv.get("investor") or {}).get("value") or \
                inv.get("investor_name") or inv.get("name")
            if n and n not in inv_names:
                inv_names.append(n)

        revenue_range = d.get("revenue_range") or d.get("revenueRange") or ""

        return _CrunchbaseData(
            rounds=rounds,
            total_funding_usd=int(total_usd) if total_usd else None,
            investors=inv_names,
            revenue_range=str(revenue_range) if revenue_range else "",
        )

    def fetch_crunchbase_data(
        self, company_name: str, known_signals: Optional[Dict] = None
    ) -> "_CrunchbaseData":
        """
        Fetch funding data from Crunchbase.
        Tries pratikdani Apify actor first; falls back to Firecrawl AI extraction.
        Returns a _CrunchbaseData with rounds, total_funding_usd, and investors.

        known_signals: optional dict with any of:
          website_url, linkedin_slug, founded_year, hq_country, industry, description
        These are passed to CrunchbaseSlugResolver to disambiguate the correct
        company page. Providing website_url is the strongest possible signal.
        """
        # Resolve the correct Crunchbase slug before scraping
        from .crunchbase_slug_resolver import CrunchbaseSlugResolver

        signals = known_signals or {}
        try:
            resolver = CrunchbaseSlugResolver(apify_client=self._apify)
            resolution = resolver.resolve_slug(company_name, signals)

            if resolution.confidence < 0.7:
                logger.warning(
                    "Crunchbase slug for %r could not be resolved with confidence (%.2f, method=%s) — skipping",
                    company_name, resolution.confidence, resolution.resolution_method,
                )
                return _CrunchbaseData()

            slug = resolution.slug
            cb_url = resolution.crunchbase_url
            logger.info(
                "Crunchbase slug resolved: %r → %s (%.2f, %s)",
                company_name, slug, resolution.confidence, resolution.resolution_method,
            )
        except Exception as _e:
            logger.warning(
                "Slug resolver failed for %r: %s — using derived slug", company_name, _e
            )
            slug = self._cb_slug(company_name)
            cb_url = f"https://www.crunchbase.com/organization/{slug}"

        # --- Apify: pratikdani bulk actor ---
        if self._apify:
            try:
                items = self._apify.run_actor(
                    "pratikdani~crunchbase-companies-bulk-scraper-no-cookies",
                    {"urls": [cb_url]},
                )
                if items and isinstance(items[0], dict):
                    data = self._parse_pratikdani(items[0])
                    logger.info(
                        "Crunchbase Apify: %d rounds, $%s total for %r",
                        len(data.rounds),
                        f"{data.total_funding_usd:,}" if data.total_funding_usd else "?",
                        company_name,
                    )
                    return data
            except Exception as e:
                logger.warning("Crunchbase Apify failed for %r (%s) — trying Firecrawl", company_name, e)

        # --- Fallback: Firecrawl AI extraction ---
        if not self._firecrawl:
            return _CrunchbaseData()

        schema = {
            "type": "object",
            "properties": {
                "funding_rounds": {
                    "type": "array",
                    "description": "All funding rounds listed on the page",
                    "items": {
                        "type": "object",
                        "properties": {
                            "round_type":     {"type": "string"},
                            "amount_usd":     {"type": "number"},
                            "announced_date": {"type": "string"},
                            "lead_investors": {"type": "array", "items": {"type": "string"}},
                        },
                    },
                },
                "total_funding_usd": {"type": "number"},
                "investors":         {"type": "array", "items": {"type": "string"}},
            },
        }
        try:
            result = self._firecrawl.scrape_url(
                cb_url,
                formats=["extract"],
                only_main_content=False,
                extract_schema=schema,
                extract_prompt="Extract all funding rounds, total funding, and investor names.",
            )
            data      = result.get("data") or result
            extracted = data.get("extract") or {}

            rounds: List[FundingRound] = []
            for fr in extracted.get("funding_rounds") or []:
                amount = fr.get("amount_usd")
                r = FundingRound(
                    round_type     = fr.get("round_type", ""),
                    amount_usd     = int(amount) if amount else None,
                    announced_date = fr.get("announced_date", ""),
                    lead_investors = fr.get("lead_investors") or [],
                    source         = "crunchbase_firecrawl",
                    confidence     = "high",
                )
                if not r.amount_usd:
                    r.issues.append("Amount not disclosed")
                rounds.append(r)

            total = extracted.get("total_funding_usd")
            logger.info("Crunchbase Firecrawl: %d rounds for %r", len(rounds), company_name)
            return _CrunchbaseData(
                rounds=rounds,
                total_funding_usd=int(total) if total else None,
                investors=extracted.get("investors") or [],
            )
        except Exception as e:
            logger.error("Crunchbase Firecrawl scrape failed for %r: %s", company_name, e)
            return _CrunchbaseData()

    def fetch_from_crunchbase(self, company_name: str) -> List[FundingRound]:
        """Backwards-compatible wrapper — returns only the rounds list."""
        return self.fetch_crunchbase_data(company_name).rounds

    # ------------------------------------------------------------------ #
    #  Deduplication                                                       #
    # ------------------------------------------------------------------ #

    def _deduplicate(self, rounds: List[FundingRound]) -> List[FundingRound]:
        """Dedup by (round_type, year-month). Crunchbase entries win over SERP."""
        seen: Dict[tuple, FundingRound] = {}
        for r in rounds:
            date_key = r.announced_date[:7] if r.announced_date else ""
            key = (r.round_type.lower(), date_key)
            if key not in seen:
                seen[key] = r
            elif r.source == "crunchbase":
                # Crunchbase data is more reliable — overwrite
                seen[key] = r
        return sorted(seen.values(), key=lambda x: x.announced_date or "", reverse=True)

    # ------------------------------------------------------------------ #
    #  Public entry point                                                  #
    # ------------------------------------------------------------------ #

    def extract(self, company_name: str, url: str = "") -> List[Dict]:
        """
        Run full funding pipeline: Crunchbase (Apify) → SERP (HasData).
        Returns deduplicated list sorted newest first.
        """
        crunchbase_rounds = self.fetch_from_crunchbase(company_name)
        serp_rounds       = self.fetch_from_serp(company_name)

        # Crunchbase first so it wins deduplication
        all_rounds = self._deduplicate(crunchbase_rounds + serp_rounds)
        logger.info("Found %d funding rounds for %r", len(all_rounds), company_name)
        return [r.to_dict() for r in all_rounds]
