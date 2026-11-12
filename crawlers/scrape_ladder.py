"""Escalating crawl ladder — the console's one way to get pages off a website.

The console tries the cheapest crawler that can do the job and only climbs when
what came back isn't enough:

    1. scrapy     free, polite, fast — handles the great majority of sites
    2. crawlee    retrying browser sessions — handles JavaScript and soft blocks
    3. browser    Playwright — deterministic real-browser fallback

"Enough" is decided by :func:`assess`, never by a model. A tier's harvest
passes when it produced enough pages and text **and** carries a textual signal
for every field the caller still needs. That second half is what closes the
loop with the Agent OS: when verification finds `contact_email` missing, the
next crawl is told to look for it, the signal test fails on the thin scrapy
harvest, and the ladder climbs until a tier actually surfaces the address.

Nothing here extracts or interprets anything — it returns pages. Extraction,
verification and storage stay in the agent.

Availability: every tier is optional. A tier whose library isn't installed (or
whose API key isn't set) is skipped and reported as unavailable, so the ladder
degrades instead of failing. :func:`tier_status` powers the UI badges.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import sys
import tempfile
import time
from dataclasses import dataclass, field
from typing import Callable, Optional
from urllib.parse import urlparse

from .url_safety import UnsafeURL, ensure_public_url, get_with_safe_redirects, same_site

_crawl_context = threading.local()


class CrawlCancelled(RuntimeError):
    pass

ALL_TIERS = ("scrapy", "crawlee", "browser", "firecrawl")
DEFAULT_ORDER = ALL_TIERS          # cheapest first; the operator can rearrange
TIER_LABELS = {
    "scrapy": "Scrapy",
    "crawlee": "Crawlee",
    "browser": "Playwright",
    "firecrawl": "Firecrawl",
}
# Firecrawl is the only tier that bills per page and the only one that is not
# our own crawler, so it sits last: it runs when the three local tiers have
# all come back short, and only when a key is configured.
PAID_TIERS = frozenset({"firecrawl"})


def configured_order() -> tuple[str, ...]:
    """The escalation order the operator arranged, from
    ``STARTLINE_LADDER_ORDER`` (comma-separated tier names).

    Unknown names are ignored and missing ones are appended in their default
    position, so a stale or hand-edited value can never drop a tier from the
    ladder or crash a crawl.
    """
    raw = (os.environ.get("STARTLINE_LADDER_ORDER") or "").strip()
    if not raw:
        return DEFAULT_ORDER
    chosen = [t.strip().lower() for t in raw.split(",") if t.strip().lower() in ALL_TIERS]
    seen = set(chosen)
    return tuple(chosen + [t for t in DEFAULT_ORDER if t not in seen])


def disabled_tiers() -> set[str]:
    """Tiers switched off in the flow, from ``STARTLINE_LADDER_DISABLED``."""
    raw = (os.environ.get("STARTLINE_LADDER_DISABLED") or "").strip()
    return {t.strip().lower() for t in raw.split(",") if t.strip().lower() in ALL_TIERS}


def active_order() -> tuple[str, ...]:
    """The tiers a crawl will actually walk, in order."""
    off = disabled_tiers()
    return tuple(t for t in configured_order() if t not in off)


# Back-compat: TIERS used to be the fixed order. Anything reading it now gets
# the configured one — it is a module-level name, so it is resolved per call
# through this module's __getattr__.
def __getattr__(name):
    if name == "TIERS":
        return configured_order()
    raise AttributeError(name)

DEFAULT_MAX_PAGES = 12
SUBPROCESS_TIMEOUT = 180        # seconds per out-of-process tier

# --- link scoring -----------------------------------------------------------
# Mirrors crawlers/scrapy_worker.py (which keeps its own copy because it
# runs as a standalone subprocess) and the agent's site_adapter, so every tier
# prefers the same high-value pages, in English and Korean.

PAGE_SIGNALS = [(re.compile(p, re.I), w) for p, w in [
    (r"about|company|who[-_ ]?we|overview|소개|회사소개|기업소개|인사말|연혁", 10),
    (r"team|people|leadership|founders|executives|경영진|임원|팀|구성원|조직", 9),
    (r"products?|services?|solutions?|portfolio|catalog|제품|서비스|솔루션|사업", 9),
    (r"pricing|prices?|plans?|fees?|요금|가격|플랜|견적", 10),
    (r"contact|inquiry|support|찾아오시는|오시는길|문의|연락처|고객센터", 8),
    (r"press|news(room)?|media|\bir\b|investor|보도|뉴스|공지|투자", 5),
    (r"download|brochure|catalog|spec|자료|카탈로그|브로슈어", 6),
    # legal/imprint pages: dull, but often the only place a site states its
    # registered entity, address and a real email (an EU Impressum must).
    (r"imprint|impressum|legal[-_ ]?notice|privacy|terms|사업자정보|법적고지", 6),
]]

SKIP_LINK = re.compile(
    r"\.(jpe?g|png|gif|webp|svg|ico|css|js|zip|mp4|mp3)(\?|$)|"
    r"^(mailto|tel|javascript):|/(login|signin|sign-in|signup|sign-up|register|logout|forgot|forget|password|reset|cart|checkout|my-?page)",
    re.I)


def page_score(path: str, anchor_text: str = "") -> int:
    blob = f"{path} {anchor_text}"
    return sum(w for pat, w in PAGE_SIGNALS if pat.search(blob))


# --- sufficiency thresholds -------------------------------------------------
MIN_PAGES = 2                   # a homepage alone is not a harvest
MIN_TOTAL_CHARS = 3_000         # visible text across all pages
MIN_PAGE_CHARS = 400            # a page below this is boilerplate, not content
BLOCK_PAGE = re.compile(
    r"captcha|verify you are human|checking your browser|access denied|"
    r"temporarily blocked|unusual traffic|enable javascript and cookies|"
    r"cloudflare ray id|security challenge|request unsuccessful|"
    # Cloudflare's current interstitial says none of the above. A browser tier
    # renders it happily and returns a few hundred characters of challenge
    # text, which without this would be stored as though it were the page.
    r"just a moment\s*\.\.\.|performing security verification|"
    r"security service to protect (itself|against)|"
    r"needs to review the security of your connection|"
    r"vercel security checkpoint|attention required",
    re.I,
)
SOFT_404 = re.compile(
    r"\b(page|content|resource)\s+(was\s+)?not\s+found\b|"
    r"\b404\b.{0,80}\b(not found|error)\b|"
    r"요청하신\s*페이지를\s*찾을\s*수\s*없", re.I | re.S,
)


def is_block_page(page: Page) -> bool:
    """Reject challenge/login shells that otherwise pass a character count."""
    text = page.text[:8000]
    return bool((BLOCK_PAGE.search(text) or SOFT_404.search(text))
                and len(text) < 6000)

# Text that states a hard fact. Used to protect footer lines from being
# mistaken for page template — narrower than FIELD_SIGNALS on purpose, because
# several of those are catch-alls (brand_name and domain are both r"\w").
HARD_FACT = re.compile(
    r"[\w.+-]+@[\w-]+\.[\w.]+"                        # email
    r"|\+?\d[\d\s().-]{7,}\d"                         # phone
    r"|(founded|established|since|설립|창립)\D{0,20}(18|19|20)\d{2}"
    r"|(headquarters?|본사|소재지)"
    r"|\b(사업자등록번호|대표자|법인등록번호)\b",
    re.I,
)

# What a field looks like in raw page text. Used to decide whether a harvest
# can plausibly answer for a field the Agent OS reported missing. Deliberately
# loose: this gates *escalation*, not extraction — the cleaner and verifier
# still decide what is true.
FIELD_SIGNALS: dict[str, re.Pattern] = {
    "contact_email": re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"),
    "contact_phone": re.compile(r"(\+?\d[\d\s().-]{7,}\d)|전화|연락처|Tel\.?", re.I),
    "employee_count": re.compile(
        r"\b\d{1,6}\s*(\+|명|employees?|people|staff|team members?)\b", re.I),
    "founded_year": re.compile(
        r"\b(18|19|20)\d{2}\b.{0,40}(founded|established|설립|창립)|"
        r"(founded|established|설립|창립).{0,40}\b(18|19|20)\d{2}\b", re.I),
    "hq_location": re.compile(
        r"(headquarters?|HQ|address|본사|주소|소재지)|\b(Seoul|Busan|서울|경기|부산)\b", re.I),
    "ceo_name": re.compile(r"\b(CEO|Chief Executive|대표이사|대표|founder)\b", re.I),
    "key_people": re.compile(
        r"\b(CEO|CTO|CFO|COO|founder|co-founder|대표|이사|팀장)\b", re.I),
    "funding_stage": re.compile(
        r"\b(pre-?seed|seed|series\s*[a-e]|IPO|funding|투자유치|시리즈)\b", re.I),
    "funding_total_usd": re.compile(r"(\$|USD)\s?\d", re.I),
    "funding_total_krw": re.compile(r"(₩|KRW|억원|만원|원)\s?\d|\d\s?(억|만)\s?원", re.I),
    "annual_revenue_krw": re.compile(r"(revenue|매출|매출액)", re.I),
    "pricing_summary": re.compile(
        r"(pricing|price|plan|/mo|per month|요금|가격|월\s?\d)", re.I),
    "products": re.compile(r"(products?|services?|solutions?|제품|서비스|솔루션)", re.I),
    "product_descriptions": re.compile(
        r"(products?|services?|solutions?|제품|서비스|솔루션)", re.I),
    "product_image_urls": re.compile(r"IMAGE:\s*https?://|<img", re.I),
    "brochure_urls": re.compile(r"\.pdf\b|brochure|catalog|카탈로그|브로슈어", re.I),
    "industry": re.compile(r"(industry|sector|업종|산업|분야)", re.I),
    "social_profiles": re.compile(
        r"https?://(?:[^/]+\.)?(linkedin|facebook|instagram|whatsapp|twitter|youtube|tiktok|threads|github|pinterest)\.",
        re.I),
    "description": re.compile(r"\w{200,}", re.S),   # any substantial prose
    "legal_name": re.compile(r"(Co\.,? ?Ltd|Inc\.|주식회사|\(주\)|유한회사)", re.I),
    "brand_name": re.compile(r"\w"),
    "domain": re.compile(r"\w"),
    "company_size_bucket": re.compile(
        r"\b\d{1,6}\s*(\+|명|employees?|people|staff)\b", re.I),
}


# ---------------------------------------------------------------------------
# results
# ---------------------------------------------------------------------------

@dataclass
class Page:
    url: str
    html: str

    @property
    def text(self) -> str:
        """Visible text, cached. Used by the sufficiency gate only.

        Space-joined on purpose: the field signals match phrases like
        "founded … 1998" that straddle adjacent text nodes, and `.` in those
        patterns will not cross a newline.
        """
        cached = getattr(self, "_text", None)
        if cached is None:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(self.html or "", "lxml")
            for tag in soup(["script", "style", "noscript"]):
                tag.decompose()
            cached = soup.get_text(" ", strip=True)
            object.__setattr__(self, "_text", cached)
        return cached

    @property
    def blocks(self) -> list[str]:
        """One entry per text node — the view template detection needs."""
        cached = getattr(self, "_blocks", None)
        if cached is None:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(self.html or "", "lxml")
            for tag in soup(["script", "style", "noscript"]):
                tag.decompose()
            cached = [b.strip() for b in soup.get_text("\n", strip=True).split("\n")
                      if b.strip()]
            object.__setattr__(self, "_blocks", cached)
        return cached


@dataclass
class Attempt:
    tier: str
    ok: bool                     # did this tier satisfy the gate?
    pages: int
    chars: int
    seconds: float
    missing_signals: list[str] = field(default_factory=list)
    error: Optional[str] = None
    skipped: Optional[str] = None    # why the tier never ran

    def to_dict(self) -> dict:
        return {"tier": self.tier, "label": TIER_LABELS[self.tier], "ok": self.ok,
                "pages": self.pages, "chars": self.chars,
                "seconds": round(self.seconds, 1),
                "missing_signals": self.missing_signals,
                "error": self.error, "skipped": self.skipped}


@dataclass
class LadderResult:
    base_url: Optional[str]
    pages: list[Page]
    tier: Optional[str]                  # the tier whose harvest we kept
    satisfied: bool                      # did that harvest pass the gate?
    attempts: list[Attempt]
    missing_signals: list[str]           # still unanswered after the last tier

    def to_dict(self) -> dict:
        return {"base_url": self.base_url, "tier": self.tier,
                "satisfied": self.satisfied, "page_count": len(self.pages),
                "chars": sum(len(p.text) for p in self.pages),
                "missing_signals": self.missing_signals,
                "attempts": [a.to_dict() for a in self.attempts]}


# ---------------------------------------------------------------------------
# availability
# ---------------------------------------------------------------------------

def _has_module(name: str) -> bool:
    import importlib.util
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _has_crawlee_browser() -> bool:
    try:
        from crawlee.crawlers import PlaywrightCrawler  # noqa: F401
        return True
    except ImportError:
        return False


def tier_status() -> list[dict]:
    """Per-tier availability in the arranged order, for the flow chart and the
    settings panel. `enabled` is the operator's switch; `available` is whether
    the machine can actually run it."""
    out = []
    off = disabled_tiers()
    for tier in configured_order():
        available, detail = True, ""
        if tier == "scrapy":
            available = _has_module("scrapy")
            detail = "pip install scrapy" if not available else "installed"
        elif tier == "crawlee":
            available = _has_crawlee_browser()
            detail = ("installed with Playwright support" if available
                      else "pip install crawlee[playwright]")
        elif tier == "browser":
            available = _has_module("playwright")
            detail = "Playwright" if available else "pip install playwright"
        elif tier == "firecrawl":
            available = bool(os.environ.get("FIRECRAWL_API_KEY"))
            detail = ("hosted renderer, billed per page" if available
                      else "set FIRECRAWL_API_KEY in credentials.env")
        out.append({"tier": tier, "label": TIER_LABELS[tier],
                    "available": available, "detail": detail,
                    "enabled": tier not in off, "paid": tier in PAID_TIERS})
    return out


# ---------------------------------------------------------------------------
# the sufficiency gate
# ---------------------------------------------------------------------------

# Pages in the same directory are judged more harshly: that is where templated
# permutations (one page per plan/period/colour) live.
SIBLING_DUP_THRESHOLD = 0.65

def _shingles(text: str, size: int = 8) -> set[str]:
    words = re.findall(r"\w+", text.lower())
    if len(words) < size:
        return {" ".join(words)} if words else set()
    return {" ".join(words[i:i + size]) for i in range(len(words) - size + 1)}


def _lang_key(path: str) -> tuple[str, Optional[str]]:
    """Split a leading language segment off a path.

    ``/en/about`` and ``/zh/about`` are the same page written twice. They are
    not textually similar (different scripts), so near-duplicate detection
    cannot catch them — but they cost two crawls and two extractions, and the
    extractor may then quote the Chinese one as evidence for an English field.
    """
    m = re.match(r"^/([a-z]{2}(?:-[a-z]{2})?)(/.*)?$", path.lower())
    if m and m.group(1) not in ("us", "co", "io"):
        return (m.group(2) or "/"), m.group(1)
    return path.lower() or "/", None


def dedupe_pages(pages: list[Page], threshold: float = 0.8,
                 prefer_lang: str = "en") -> list[Page]:
    """Drop pages that repeat a page already kept.

    Two shapes of waste, both measured on real sites:

    * **Templated permutations** — ``/product/growth-annual``,
      ``/product/growth-monthly``, ``/product/business-annual`` … are one page
      with a price swapped. Six of them took 69% of one crawl's budget and
      taught the extractor that "Growth" was a product.
    * **Language variants** — ``/en/about`` and ``/zh/about``.

    Kept in crawl order, so the highest-scoring page of a family survives.
    """
    kept: list[Page] = []
    kept_shingles: list[set[str]] = []
    seen_paths: dict[str, int] = {}          # language-stripped path -> index

    for page in pages:
        path = urlparse(page.url).path or "/"
        base_path, lang = _lang_key(path)

        prior = seen_paths.get(base_path)
        if prior is not None:
            # same page in another language — keep the preferred one
            _, kept_lang = _lang_key(urlparse(kept[prior].url).path or "/")
            if lang == prefer_lang and kept_lang != prefer_lang:
                kept[prior] = page
                kept_shingles[prior] = _shingles(page.text)
            continue

        sh = _shingles(page.text)
        family = base_path.rsplit("/", 1)[0] or "/"
        duplicate = False
        if sh:
            for idx, other in enumerate(kept_shingles):
                if not other:
                    continue
                overlap = len(sh & other) / max(len(sh | other), 1)
                # Siblings in one directory are where templated permutations
                # live, and they land just under the global bar — the six
                # /product/<plan>-<period> pages measured 0.75–0.79 against each
                # other. Judge those on a tighter threshold than two unrelated
                # pages that happen to share boilerplate.
                other_family = (_lang_key(urlparse(kept[idx].url).path or "/")[0]
                                .rsplit("/", 1)[0] or "/")
                bar = SIBLING_DUP_THRESHOLD if family == other_family else threshold
                if overlap >= bar:
                    duplicate = True
                    break
        if duplicate:
            continue

        seen_paths[base_path] = len(kept)
        kept.append(page)
        kept_shingles.append(sh)
    return kept


def _content_texts(pages: list[Page]) -> list[str]:
    """Per-page text with the site template removed.

    A line appearing on most pages of a site is chrome regardless of the markup
    that produced it — which matters because modern sites build navigation from
    plain <div>s, so stripping <nav>/<header>/<footer> catches nothing.
    """
    if len(pages) < 3:
        return [p.text for p in pages]

    counts: dict[str, int] = {}
    for page in pages:
        for block in set(page.blocks):
            counts[block] = counts.get(block, 0) + 1

    # A repeated line carrying a hard fact is not template — footers are exactly
    # where "Founded 1998", a company email or a registered address live, and
    # they repeat on every page by design.
    #
    # Deliberately NOT reusing FIELD_SIGNALS here: several of those are
    # catch-alls (brand_name and domain are both r"\w"), so every nav item would
    # count as data-bearing and nothing would ever be stripped.
    threshold = max(3, int(len(pages) * 0.6))
    chrome = {b for b, n in counts.items()
              if n >= threshold and len(b) <= 120 and not HARD_FACT.search(b)}
    if not chrome:
        return [p.text for p in pages]

    # Re-joined with spaces so the field-signal patterns still match across
    # what used to be neighbouring nodes. If a page is *mostly* repeated text
    # the heuristic has misfired (near-duplicate pages, or a one-line site) —
    # keep the original rather than delete the page's only content.
    out = []
    for page in pages:
        kept = " ".join(b for b in page.blocks if b not in chrome)
        out.append(page.text if len(kept) < len(page.text) * 0.3 else kept)
    return out


def assess(pages: list[Page], want_fields: Optional[list[str]] = None
           ) -> tuple[bool, list[str]]:
    """Is this harvest enough? Returns (ok, missing_signals).

    Two independent tests, both code:
      * bulk — enough pages carrying enough text to be worth extracting from
      * signals — for every field the caller still needs, some page must
        contain text that could plausibly answer it
    """
    # Measure content, not template. Nav and footer repeat on every page, so a
    # site with almost nothing to say can clear MIN_TOTAL_CHARS on chrome alone
    # and be judged a sufficient harvest.
    texts = _content_texts(pages)
    solid = [t for t in texts if len(t) >= MIN_PAGE_CHARS]
    total = sum(len(t) for t in solid)
    if len(solid) < MIN_PAGES or total < MIN_TOTAL_CHARS:
        return False, list(want_fields or [])

    if not want_fields:
        return True, []

    blob = "\n".join(solid)
    missing = [f for f in want_fields
               if f in FIELD_SIGNALS and not FIELD_SIGNALS[f].search(blob)]
    return (not missing), missing


# ---------------------------------------------------------------------------
# tier implementations — each returns pages or raises
# ---------------------------------------------------------------------------

def _run_worker(script: str, url: str, max_pages: int) -> list[Page]:
    """Run one of the out-of-process crawlers and read back its pages."""
    worker = os.path.join(os.path.dirname(os.path.abspath(__file__)), script)
    fd, out_path = tempfile.mkstemp(suffix=".json", prefix="crawl_")
    os.close(fd)
    try:
        proc = subprocess.Popen(
            [sys.executable, worker, url, out_path, str(max_pages)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            cwd=tempfile.gettempdir(),      # keep crawler scratch out of the repo
        )
        deadline = time.time() + SUBPROCESS_TIMEOUT
        while proc.poll() is None:
            check = getattr(_crawl_context, "cancel_check", None)
            if check and check():
                proc.kill()
                proc.communicate()
                raise CrawlCancelled("crawl cancelled by operator")
            if time.time() >= deadline:
                proc.kill()
                proc.communicate()
                raise RuntimeError("crawler subprocess timed out")
            time.sleep(0.15)
        _stdout, stderr = proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError((stderr or "crawler failed").strip()[:300])
        with open(out_path, encoding="utf-8") as f:
            raw = json.load(f)
    finally:
        try:
            os.remove(out_path)
        except OSError:
            pass
    return [Page(url=r["url"], html=r.get("html") or "") for r in raw]


def crawl_scrapy(base_url: str, max_pages: int) -> list[Page]:
    return _run_worker("scrapy_worker.py", base_url, max_pages)


def crawl_crawlee(base_url: str, max_pages: int) -> list[Page]:
    return _run_worker("crawlee_worker.py", base_url, max_pages)


def crawl_browser(base_url: str, max_pages: int) -> list[Page]:
    """Render with Playwright.

    Discovery is the cheap part — links come from the rendered homepage, then
    the top-scoring ones are rendered too.
    """
    ensure_public_url(base_url)
    render, close = _browser_renderer()
    try:
        check = getattr(_crawl_context, "cancel_check", None)
        if check and check():
            raise CrawlCancelled("crawl cancelled by operator")
        home = render(base_url)
        if not home:
            return []
        pages = [Page(url=base_url, html=home)]

        from bs4 import BeautifulSoup
        from urllib.parse import urljoin
        host = urlparse(base_url).netloc
        scored: dict[str, int] = {}
        for a in BeautifulSoup(home, "lxml").find_all("a", href=True):
            href = a["href"].strip()
            if not href or SKIP_LINK.search(href):
                continue
            url = urljoin(base_url, href)
            p = urlparse(url)
            if p.netloc != host or p.scheme not in ("http", "https"):
                continue
            try:
                ensure_public_url(url)
            except UnsafeURL:
                continue
            s = page_score(p.path, a.get_text(" ", strip=True)[:80])
            if s > 0:
                key = f"{p.scheme}://{p.netloc}{p.path.rstrip('/')}"
                scored[key] = max(scored.get(key, 0), s)

        for url, _s in sorted(scored.items(), key=lambda kv: kv[1], reverse=True):
            if check and check():
                raise CrawlCancelled("crawl cancelled by operator")
            if len(pages) >= max_pages:
                break
            html = render(url)
            if html:
                pages.append(Page(url=url, html=html))
        return pages
    finally:
        close()


def _browser_renderer() -> tuple[Callable[[str], Optional[str]], Callable[[], None]]:
    """Return the Playwright renderer and its cleanup callback."""
    if _has_module("playwright"):
        import startline_agent  # noqa: F401  (puts the agent modules on sys.path)
        from js_site_adapter import PlaywrightRenderer
        r = PlaywrightRenderer()
        return r.render, r.close

    raise RuntimeError("no browser backend: pip install playwright")


def crawl_firecrawl(base_url: str, max_pages: int) -> list[Page]:
    """Fetch through Firecrawl's hosted renderer.

    Last resort: it is billed per page and run by someone else, so it only
    sees sites the local tiers could not read. Discovery uses the link list
    Firecrawl already returns for the first page, scored with the same
    `page_score` the other tiers use, so the pages it pays for are the same
    ones the rest of the ladder would have chosen.
    """
    ensure_public_url(base_url)
    from .firecrawl_client import FirecrawlClient
    key = os.environ.get("FIRECRAWL_API_KEY", "")
    if not key:
        raise RuntimeError("no Firecrawl API key")
    client = FirecrawlClient(key)

    def _fetch(url: str) -> tuple[Optional[str], list[str]]:
        result = client.scrape_url(url, formats=["html", "links"])
        data = result.get("data") or result
        html = data.get("html") or data.get("rawHtml") or ""
        links = [l for l in (data.get("links") or []) if isinstance(l, str)]
        return (html or None), links

    check = getattr(_crawl_context, "cancel_check", None)
    if check and check():
        raise CrawlCancelled("crawl cancelled by operator")
    home, links = _fetch(base_url)
    if not home:
        return []
    pages = [Page(url=base_url, html=home)]

    host = urlparse(base_url).netloc
    scored: dict[str, int] = {}
    for href in links:
        if SKIP_LINK.search(href):
            continue
        parsed = urlparse(href)
        if parsed.netloc != host or parsed.scheme not in ("http", "https"):
            continue
        try:
            ensure_public_url(href)
        except UnsafeURL:
            continue
        score = page_score(parsed.path)
        if score > 0:
            key_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path.rstrip('/')}"
            scored[key_url] = max(scored.get(key_url, 0), score)

    for url, _score in sorted(scored.items(), key=lambda kv: kv[1], reverse=True):
        if check and check():
            raise CrawlCancelled("crawl cancelled by operator")
        if len(pages) >= max_pages:
            break
        try:
            html, _ = _fetch(url)
        except Exception:                        # noqa: BLE001 — one bad page
            continue
        if html:
            pages.append(Page(url=url, html=html))
    return pages


TIER_FUNCS: dict[str, Callable[[str, int], list[Page]]] = {
    "scrapy": crawl_scrapy,
    "crawlee": crawl_crawlee,
    "browser": crawl_browser,
    "firecrawl": crawl_firecrawl,
}


# ---------------------------------------------------------------------------
# the ladder
# ---------------------------------------------------------------------------

def resolve_base(domain_or_url: str) -> Optional[str]:
    """Find the working scheme/host for a bare domain (or normalize a URL)."""
    import requests
    if "://" in domain_or_url:
        candidates = [domain_or_url]
    else:
        d = domain_or_url.strip().strip("/")
        candidates = [f"https://{d}", f"https://www.{d}", f"http://{d}"]
    for cand in candidates:
        try:
            ensure_public_url(cand)
            r = get_with_safe_redirects(
                requests.Session(), cand, timeout=12,
                headers={"User-Agent": "StartlineBot/0.1"})
            if r.status_code < 400:
                p = urlparse(r.url)
                return f"{p.scheme}://{p.netloc}/"
        except (Exception, UnsafeURL):
            continue
    return None


def crawl(domain_or_url: str, *,
          start_tier: Optional[str] = None,
          want_fields: Optional[list[str]] = None,
          max_pages: int = DEFAULT_MAX_PAGES,
          single_tier: bool = False,
          cancel_check: Optional[Callable[[], bool]] = None,
          log: Optional[Callable[[str], None]] = None) -> LadderResult:
    """Climb the ladder until a tier's harvest satisfies :func:`assess`.

    `start_tier` is the floor — the Agent OS raises it for a company whose last
    crawl came back incomplete, so the next attempt doesn't waste time on a
    tier that already failed for that site.
    `want_fields` are the fields still missing; they make the gate stricter.
    """
    def _log(msg: str) -> None:
        if log:
            log(msg)

    base = resolve_base(domain_or_url)
    if base is None:
        _log(f"{domain_or_url}: no reachable website")
        return LadderResult(None, [], None, False, [], list(want_fields or []))

    order = active_order()
    # No start_tier means "from the beginning of the arranged order" — naming a
    # specific tier here (it used to default to "scrapy") silently skips
    # everything the operator put before it.
    try:
        floor = order.index(start_tier) if start_tier else 0
    except ValueError:
        floor = 0

    status = {t["tier"]: t for t in tier_status()}
    attempts: list[Attempt] = []
    best: tuple[int, list[Page], str] | None = None      # (chars, pages, tier)
    last_missing: list[str] = list(want_fields or [])
    harvested: list[Page] = []       # union of every tier's pages, deduped
    _crawl_context.cancel_check = cancel_check

    tiers_to_run = order[floor:floor + 1] if single_tier else order[floor:]
    for tier in tiers_to_run:
        if cancel_check and cancel_check():
            raise CrawlCancelled("crawl cancelled by operator")
        if not status[tier]["available"]:
            attempts.append(Attempt(tier, False, 0, 0, 0.0,
                                    skipped=status[tier]["detail"]))
            _log(f"{tier}: skipped ({status[tier]['detail']})")
            continue

        started = time.time()
        try:
            pages = TIER_FUNCS[tier](base, max_pages)
        except Exception as e:
            attempts.append(Attempt(tier, False, 0, 0, time.time() - started,
                                    error=str(e)[:300]))
            _log(f"{tier}: failed — {e}")
            continue

        raw_count = len(pages)
        pages = [page for page in pages if not is_block_page(page)]
        pages = dedupe_pages(pages)
        if raw_count != len(pages):
            _log(f"{tier}: {raw_count} pages -> {len(pages)} after dropping "
                 "near-duplicate and translated copies")

        # Every tier's harvest is kept, not just the winning one. A weaker tier
        # frequently reaches a page a later tier misses, and discarding it threw
        # away pages the extractor had never seen. Duplicates across tiers
        # collapse here, so the union costs far less than the sum.
        merged = dedupe_pages(harvested + pages)
        gained = len(merged) - len(harvested)
        harvested = merged

        ok, missing = assess(harvested, want_fields)
        chars = sum(len(p.text) for p in harvested)
        attempts.append(Attempt(tier, ok, len(pages), chars, time.time() - started,
                                missing_signals=missing))
        _log(f"{tier}: {len(pages)} pages ({gained} new), {chars:,} chars total"
             + (" — enough" if ok else f" — missing {', '.join(missing) or 'content'}"))

        if best is None or chars > best[0]:
            best = (chars, list(harvested), tier)
        last_missing = missing

        if ok:
            return LadderResult(base, harvested, tier, True, attempts, [])

    # nothing satisfied the gate — hand over everything every tier found
    if harvested:
        return LadderResult(base, harvested, best[2] if best else None,
                            False, attempts, last_missing)
    return LadderResult(base, [], None, False, attempts, last_missing)


def retrieve_url(url: str, *, start_tier: Optional[str] = None,
                 cancel_check: Optional[Callable[[], bool]] = None) -> LadderResult:
    """Retrieve one known URL, escalating transport without rediscovering a site."""
    _crawl_context.cancel_check = cancel_check
    order = active_order()
    try:
        floor = order.index(start_tier) if start_tier else 0
    except ValueError:
        floor = 0
    status = {item["tier"]: item for item in tier_status()}
    attempts: list[Attempt] = []
    for tier in order[floor:]:
        if cancel_check and cancel_check():
            raise CrawlCancelled("crawl cancelled by operator")
        if not status[tier]["available"]:
            attempts.append(Attempt(tier, False, 0, 0, 0.0,
                                    skipped=status[tier]["detail"]))
            continue
        started = time.time()
        try:
            pages = TIER_FUNCS[tier](url, 1)
            pages = [page for page in pages if not is_block_page(page)]
        except Exception as exc:
            attempts.append(Attempt(tier, False, 0, 0, time.time() - started,
                                    error=str(exc)[:300]))
            continue
        chars = sum(len(page.text) for page in pages)
        ok = bool(pages and chars >= 100)
        attempts.append(Attempt(tier, ok, len(pages), chars,
                                time.time() - started))
        if ok:
            return LadderResult(url, pages[:1], tier, True, attempts, [])
    return LadderResult(url, [], None, False, attempts, [])


def next_tier(tier: Optional[str]) -> Optional[str]:
    """The tier after `tier` in the arranged order, or None at the end."""
    order = active_order() or DEFAULT_ORDER
    if tier is None:
        return order[0]
    try:
        i = order.index(tier)
    except ValueError:
        return order[0]
    return order[i + 1] if i + 1 < len(order) else None
