"""Scrapy crawl worker — ladder tier 1, run as a subprocess.

Scrapy owns a Twisted reactor that cannot be started twice in one process, so
the ladder never imports this module: it runs it as

    python crawlers/scrapy_worker.py <start_url> <out.json> [max_pages]

and reads the JSON array of {url, html} it writes. Same-origin only, robots
respected, high-value pages first (the link scoring mirrors the agent's
site_adapter so both crawlers prefer about/team/products/pricing/contact
pages, in English and Korean).
"""

from __future__ import annotations

import json
import re
import sys
from urllib.parse import urlparse

from url_safety import UnsafeURL, ensure_public_url, same_site

import scrapy
from scrapy.crawler import CrawlerProcess

# (pattern, weight) — matched against the URL path and the anchor text.
PAGE_SIGNALS = [(re.compile(p, re.I), w) for p, w in [
    (r"about|company|who[-_ ]?we|overview|소개|회사소개|기업소개|인사말|연혁", 10),
    (r"team|people|leadership|founders|executives|경영진|임원|팀|구성원|조직", 9),
    (r"products?|services?|solutions?|portfolio|catalog|제품|서비스|솔루션|사업", 9),
    (r"pricing|prices?|plans?|fees?|요금|가격|플랜|견적", 10),
    (r"contact|inquiry|support|찾아오시는|오시는길|문의|연락처|고객센터", 8),
    (r"press|news(room)?|media|ir\b|investor|보도|뉴스|공지|투자", 5),
    (r"download|brochure|catalog|spec|자료|카탈로그|브로슈어", 6),
    # legal/imprint pages: dull, but often the only place a site states its
    # registered entity, address and a real email (an EU Impressum must).
    (r"imprint|impressum|legal[-_ ]?notice|privacy|terms|사업자정보|법적고지", 6),
]]

SKIP = re.compile(
    r"\.(jpe?g|png|gif|webp|svg|ico|css|js|zip|mp4|mp3)(\?|$)|"
    r"^(mailto|tel|javascript):|/(login|signin|sign-in|signup|sign-up|register|logout|forgot|forget|password|reset|cart|checkout|my-?page)",
    re.I)


def score(path: str, anchor: str) -> int:
    blob = f"{path} {anchor}"
    return sum(w for pat, w in PAGE_SIGNALS if pat.search(blob))


class SiteSpider(scrapy.Spider):
    name = "site"
    custom_settings = {
        "ROBOTSTXT_OBEY": True,
        "DOWNLOAD_TIMEOUT": 15,
        "DOWNLOAD_MAXSIZE": 2_000_000,
        "CONCURRENT_REQUESTS_PER_DOMAIN": 2,
        "DOWNLOAD_DELAY": 0.5,
        "RETRY_TIMES": 1,
        "COOKIES_ENABLED": False,
        "LOG_LEVEL": "ERROR",
        "TELNETCONSOLE_ENABLED": False,
        "USER_AGENT": ("StartlineBot/0.1 (+https://startline.vip/bot; "
                       "company-card indexing)"),
    }

    def __init__(self, start_url: str, max_pages: int, sink: list, **kw):
        super().__init__(**kw)
        ensure_public_url(start_url)
        self.start_urls = [start_url]
        self.max_pages = max_pages
        self.sink = sink
        self.seen: set[str] = set()

    def parse(self, response):
        if not same_site(self.start_urls[0], response.url):
            return
        ctype = response.headers.get("Content-Type", b"").decode("latin-1")
        if "html" not in ctype.lower():
            return
        if len(self.sink) >= self.max_pages:
            return
        self.sink.append({"url": response.url, "html": response.text})

        # depth 1 only: score every same-origin link, follow the best ones
        candidates = []
        for a in response.css("a[href]"):
            href = (a.attrib.get("href") or "").strip()
            if not href or SKIP.search(href):
                continue
            url = response.urljoin(href)
            p = urlparse(url)
            if p.scheme not in ("http", "https") or not same_site(self.start_urls[0], url):
                continue
            key = f"{p.scheme}://{p.netloc}{p.path.rstrip('/')}"
            if key in self.seen:
                continue
            anchor = " ".join(a.css("::text").getall())[:80]
            s = score(p.path, anchor)
            self.seen.add(key)
            # Keep a small fallback lane for opaque/localized CMS routes.
            # Named high-value paths still win, but zero-score links are no
            # longer categorically invisible.
            candidates.append((s, url))

        candidates.sort(reverse=True)
        for _s, url in candidates[: self.max_pages]:
            yield scrapy.Request(url, callback=self.parse_leaf, priority=_s)

    def parse_leaf(self, response):
        """Collect a discovered page without following it any further."""
        if not same_site(self.start_urls[0], response.url):
            return
        ctype = response.headers.get("Content-Type", b"").decode("latin-1")
        if "html" not in ctype.lower() or len(self.sink) >= self.max_pages:
            return
        self.sink.append({"url": response.url, "html": response.text})


def main() -> int:
    if len(sys.argv) < 3:
        print("usage: scrapy_worker.py <start_url> <out.json> [max_pages]",
              file=sys.stderr)
        return 2
    start_url, out_path = sys.argv[1], sys.argv[2]
    max_pages = int(sys.argv[3]) if len(sys.argv) > 3 else 12

    pages: list[dict] = []
    process = CrawlerProcess(settings={"LOG_LEVEL": "ERROR"})
    process.crawl(SiteSpider, start_url=start_url, max_pages=max_pages, sink=pages)
    process.start()          # blocks until the crawl finishes

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(pages[:max_pages], f, ensure_ascii=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
