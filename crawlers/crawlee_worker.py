"""Crawlee crawl worker — ladder tier 3, run as a subprocess.

Crawlee is asyncio-native and keeps its own on-disk storage per run, so like
the Scrapy worker it runs out-of-process:

    python crawlers/crawlee_worker.py <start_url> <out.json> [max_pages]

Why it sits above Firecrawl in the ladder: Crawlee brings session rotation,
automatic retries with backoff, and a real browser pool if the HTTP pass keeps
coming back thin — it gets pages out of sites that block a plain crawler,
without paying for an API call per page.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from datetime import timedelta
from urllib.parse import urljoin, urlparse

from url_safety import UnsafeURL, ensure_public_url, same_site

SKIP = re.compile(
    r"\.(jpe?g|png|gif|webp|svg|ico|css|js|zip|mp4|mp3)(\?|$)|"
    r"^(mailto|tel|javascript):|/(login|signin|cart|checkout|privacy|terms)",
    re.I)

VALUABLE = re.compile(
    r"about|company|overview|team|people|leadership|founders|product|service|"
    r"solution|pricing|plan|contact|inquiry|press|news|investor|brochure|"
    r"소개|회사|연혁|경영진|임원|제품|서비스|솔루션|요금|가격|문의|연락처|뉴스|투자|자료",
    re.I)


async def crawl(start_url: str, max_pages: int) -> list[dict]:
    from crawlee.crawlers import PlaywrightCrawler, PlaywrightCrawlingContext

    ensure_public_url(start_url)
    pages: list[dict] = []

    crawler = PlaywrightCrawler(
        max_requests_per_crawl=max_pages * 2,
        request_handler_timeout=timedelta(seconds=30),
        max_request_retries=2,
        headless=True,
    )

    @crawler.router.default_handler
    async def handler(context: PlaywrightCrawlingContext) -> None:
        if len(pages) >= max_pages:
            return
        current_url = context.request.url
        if not same_site(start_url, current_url):
            return
        try:
            ensure_public_url(current_url)
        except UnsafeURL:
            return
        await context.page.wait_for_load_state("domcontentloaded")
        # Best-effort consent dismissal and bounded scrolling expose lazy
        # sections without allowing an unbounded interaction crawler.
        for label in ("Accept", "Accept all", "Allow all", "동의", "모두 허용"):
            try:
                button = context.page.get_by_role("button", name=label, exact=False)
                if await button.count():
                    await button.first.click(timeout=800)
                    break
            except Exception:
                pass
        for _ in range(3):
            await context.page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await context.page.wait_for_timeout(350)
        html = await context.page.content()
        pages.append({"url": context.request.url, "html": html})

        # follow same-origin, high-value links only (depth is bounded by the
        # request cap above)
        links = []
        anchors = await context.page.eval_on_selector_all(
            "a[href]",
            "els => els.map(a => ({href:a.href,text:(a.innerText||'').slice(0,120)}))",
        )
        for anchor in anchors:
            href = (anchor.get("href") or "").strip()
            if not href or SKIP.search(href):
                continue
            url = urljoin(context.request.url, href)
            p = urlparse(url)
            if p.scheme not in ("http", "https") or not same_site(start_url, url):
                continue
            score = 1 if VALUABLE.search(
                f"{p.path} {anchor.get('text', '')}") else 0
            links.append((score, url))
        if links:
            links.sort(key=lambda item: item[0], reverse=True)
            await context.add_requests([url for _score, url in links[:max_pages]])

    await crawler.run([start_url])
    return pages[:max_pages]


def main() -> int:
    if len(sys.argv) < 3:
        print("usage: crawlee_worker.py <start_url> <out.json> [max_pages]",
              file=sys.stderr)
        return 2
    start_url, out_path = sys.argv[1], sys.argv[2]
    max_pages = int(sys.argv[3]) if len(sys.argv) > 3 else 12

    pages = asyncio.run(crawl(start_url, max_pages))
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(pages, f, ensure_ascii=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
