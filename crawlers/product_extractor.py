"""ProductExtractor — finds product/service data from company websites via Firecrawl."""
import re
import logging
from typing import Dict, List, Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

PRICE_RE = re.compile(
    r"\$[\d,]+(?:\.\d{2})?(?:\s*/\s*(?:mo|month|yr|year|user|seat))?",
    re.IGNORECASE,
)
PRICING_MODEL_RE = re.compile(
    r"\b(freemium|free\s+trial|subscription|per[- ]seat|per[- ]user|"
    r"usage[- ]based|flat[- ]rate|one[- ]time|enterprise|open[- ]source|"
    r"pay[- ]as[- ]you[- ]go|tiered)\b",
    re.IGNORECASE,
)
IMG_RE  = re.compile(r"!\[.*?\]\((https?://[^\s)]+)\)")
PDF_MD_RE = re.compile(r"\[[^\]]*\]\((https?://[^\s)]*\.pdf(?:\?[^\s)]*)?)\)", re.IGNORECASE)
PDF_HTML_RE = re.compile(r'href=["\']([^"\']*\.pdf(?:\?[^"\']*)?)["\']', re.IGNORECASE)

# Pages most likely to contain product/pricing info
PRODUCT_PATHS = [
    "/products", "/product", "/pricing", "/services",
    "/solutions", "/features", "/platform", "/catalogue", "/catalog",
]

# URL fragments that indicate non-product content — exclude these even if path matches
_EXCLUDE_FRAGMENTS = frozenset({
    "/blog/", "/news/", "/press/", "/sessions/", "/customers/", "/newsroom/",
    "/legal/", "/careers/", "/jobs/", "/support/", "/docs/", "/changelog/",
    "/case-study/", "/case_study/", "/events/", "/webinar/", "/learn/",
})

# Extensions that indicate a real image vs an icon/SVG placeholder
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}


class Product:
    def __init__(
        self,
        name: str = "",
        description: str = "",
        catalogue: Optional[List[str]] = None,
        photos: Optional[List[str]] = None,
        pdf_urls: Optional[List[str]] = None,
        pricing: Optional[List[str]] = None,
        pricing_model: str = "",
        source: str = "",
    ):
        self.name = name
        self.description = description
        self.catalogue = catalogue or []
        self.photos = photos or []
        self.pdf_urls = pdf_urls or []
        self.pricing = pricing or []
        self.pricing_model = pricing_model
        self.source = source

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "catalogue": self.catalogue,
            "photos": self.photos,
            "pdf_urls": self.pdf_urls,
            "pricing": self.pricing,
            "pricing_model": self.pricing_model,
            "source": self.source,
        }


class ProductExtractor:
    """
    Uses Firecrawl to:
    1. Discover product/pricing URLs via map_site
    2. Scrape each page and parse products from markdown headings
    3. Validate photo URLs and extract pricing info
    """

    def __init__(self, firecrawl_client=None, apify_client=None):
        self._firecrawl = firecrawl_client
        self._apify = apify_client

    # ------------------------------------------------------------------ #
    #  URL discovery                                                       #
    # ------------------------------------------------------------------ #

    def _find_product_pages(self, base_url: str) -> List[str]:
        """Use Firecrawl map to find product-related URLs."""
        if not self._firecrawl:
            return []
        try:
            result = self._firecrawl.map_site(
                base_url, search="product OR pricing OR services OR solutions"
            )
            links = result.get("links", [])
            pat = re.compile(
                r"/(product|products|pricing|service|services|solution|solutions|"
                r"feature|features|platform|catalogue|catalog)",
                re.IGNORECASE,
            )
            return [
                l for l in links
                if pat.search(l) and not any(ex in l for ex in _EXCLUDE_FRAGMENTS)
            ][:8]
        except Exception as e:
            logger.debug("Sitemap failed for %s: %s", base_url, e)
            return []

    # ------------------------------------------------------------------ #
    #  Markdown parsing                                                    #
    # ------------------------------------------------------------------ #

    def _parse_markdown(self, markdown: str, page_url: str) -> List[Product]:
        """
        Parses H2/H3 headings as product names.
        Reads subsequent lines for description, pricing, and images.
        Only keeps products that have at least a description OR a price.
        """
        products: List[Product] = []
        current: Optional[Product] = None
        lines = markdown.split("\n")

        for line in lines:
            stripped = line.strip()

            # New product on each H2 or H3 heading
            h_match = re.match(r"^#{2,3}\s+(.+)", stripped)
            if h_match:
                if current and (current.description or current.pricing):
                    products.append(current)
                current = Product(name=h_match.group(1).strip()[:120], source=page_url)
                continue

            if current is None:
                continue

            # First substantial paragraph → description
            if not current.description and len(stripped) > 50 and not stripped.startswith("!"):
                current.description = stripped[:600]

            # Pricing strings
            for price in PRICE_RE.findall(stripped):
                if price not in current.pricing:
                    current.pricing.append(price)

            # Pricing model keyword
            if not current.pricing_model:
                m = PRICING_MODEL_RE.search(stripped)
                if m:
                    current.pricing_model = m.group(1)

            # Product images
            for img_url in IMG_RE.findall(stripped):
                if self._is_valid_image(img_url):
                    current.photos.append(img_url)

            # PDF links
            for pdf_url in PDF_MD_RE.findall(stripped):
                if pdf_url not in current.pdf_urls:
                    current.pdf_urls.append(pdf_url)

        if current and (current.description or current.pricing):
            products.append(current)

        # Quality filter: name must be reasonable length, not a generic heading
        _SKIP_NAMES = frozenset({
            "overview", "summary", "introduction", "conclusion", "resources",
            "related", "next steps", "get started", "learn more", "faq",
            "questions", "contact", "footer", "header", "navigation",
        })
        products = [
            p for p in products
            if 3 <= len(p.name) <= 80
            and p.name.lower().strip() not in _SKIP_NAMES
            and len(p.description or "") >= 30
        ]
        return products[:20]

    # ------------------------------------------------------------------ #
    #  Photo validation                                                    #
    # ------------------------------------------------------------------ #

    def _is_valid_image(self, url: str) -> bool:
        """Accept only URLs with known image extensions to filter out icons."""
        try:
            path = urlparse(url).path.lower()
            return any(path.endswith(ext) for ext in IMAGE_EXTS)
        except Exception:
            return False

    # ------------------------------------------------------------------ #
    #  Pricing page                                                        #
    # ------------------------------------------------------------------ #

    def extract_pricing_model(self, base_url: str) -> Dict:
        """
        Dedicated pass on /pricing to extract the pricing model and price points.
        Used separately from full product extraction when only pricing is needed.
        """
        if not self._firecrawl:
            return {}
        try:
            parsed = urlparse(base_url)
            pricing_url = f"{parsed.scheme}://{parsed.netloc}/pricing"
            result = self._firecrawl.scrape_url(pricing_url, formats=["markdown"])
            md = (result.get("data") or {}).get("markdown") or result.get("markdown", "")
            if not md:
                return {}
            model_m = PRICING_MODEL_RE.search(md)
            prices  = list(set(PRICE_RE.findall(md)))[:10]
            return {
                "pricing_model": model_m.group(1) if model_m else "",
                "price_points":  prices,
            }
        except Exception as e:
            logger.debug("Pricing page failed for %s: %s", base_url, e)
            return {}

    # ------------------------------------------------------------------ #
    #  Deduplication                                                       #
    # ------------------------------------------------------------------ #

    def _deduplicate(self, products: List[Product]) -> List[Product]:
        seen: set = set()
        out: List[Product] = []
        for p in products:
            key = p.name.lower().strip()
            if key and key not in seen:
                seen.add(key)
                out.append(p)
        return out

    # ------------------------------------------------------------------ #
    #  Public entry point                                                  #
    # ------------------------------------------------------------------ #

    def extract(self, url: str) -> List[Dict]:
        """
        Full product extraction pipeline:
        1. Discover product pages via sitemap
        2. Scrape each page and parse products
        3. Deduplicate and validate photo URLs
        Returns list of product dicts.
        """
        if not url or not self._firecrawl:
            return []

        try:
            parsed = urlparse(url)
            base = f"{parsed.scheme}://{parsed.netloc}"
        except Exception:
            return []

        # Discover product pages; fall back to well-known paths
        pages = self._find_product_pages(base)
        if not pages:
            pages = [base + path for path in PRODUCT_PATHS]

        all_products: List[Product] = []
        for page_url in pages[:10]:
            try:
                result = self._firecrawl.scrape_url(page_url, formats=["markdown", "html"])
                data_block = result.get("data") or result
                md   = data_block.get("markdown") or result.get("markdown", "")
                html = data_block.get("html") or result.get("html", "")
                if md and len(md) > 100:
                    found = self._parse_markdown(md, page_url)
                    # Also pull PDF hrefs out of raw HTML for this page
                    if html:
                        page_pdfs = [u for u in PDF_HTML_RE.findall(html) if u not in ["", "#"]]
                        for prod in found:
                            for pdf_url in page_pdfs:
                                if pdf_url not in prod.pdf_urls:
                                    prod.pdf_urls.append(pdf_url)
                    all_products.extend(found)
            except Exception as e:
                logger.debug("Product page %s failed: %s", page_url, e)

        products = self._deduplicate(all_products)
        logger.info("Found %d products for %s", len(products), url)
        return [p.to_dict() for p in products]
