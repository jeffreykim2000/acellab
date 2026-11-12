"""PitchBook company profiles as a pipeline source.

Resolution is by targeted search, not by sitemap. PitchBook's robots.txt does
advertise `sitemap.xml`, and the master is readable, but every child sitemap is
a `.xml.gz` that returns 403 to plain HTTP and defeats all three ladder tiers --
so the master yields 194 URLs that cannot be opened and there is no directory
to build. Individual profiles, however, fetch fine through the browser tier.
So this adapter finds the one profile belonging to the company being enriched
and reads that, in the same shape as `_find_linkedin_url`.

Identity is the reason this adapter is careful. `Corporation.url` on a
PitchBook profile points at the PitchBook page itself, and `sameAs` carries
social links -- neither states the company's own website, so there is nothing
in the markup to verify against. The page does print it in rendered text:

    Contact Information Website shopvcf.com Ownership Status Privately Held

`_stated_website` reads exactly that, and `fetch` returns nothing unless it
equals the domain being enriched. A name-only match is refused on purpose: a
single source now clears the verified threshold unaided, so a wrong match would
publish another company's funding as fact.

What a profile carries, all of it fetched with the console's own ladder:

    Corporation   name, description, foundingDate, numberOfEmployees, logo, sameAs
    PostalAddress streetAddress, addressLocality, addressRegion, postalCode
    FAQPage       founded / headquarters / size / industry / ownership /
                  "has raised $675M" / competitors
    rendered      Website, Ownership Status, Financing Status
"""
from __future__ import annotations

from typing import Optional

import json
import os
import re
import urllib.robotparser
from urllib.parse import urlparse

import startline_agent  # noqa: F401  (puts the agent modules on sys.path)
from orchestrator import ScrapedDoc, SourceAdapter

from . import scrape_ladder
from .cbinsights_adapter import parse_money
from .hasdata_client import HasDataClient

ROBOTS_URL = "https://pitchbook.com/robots.txt"
USER_AGENT = "StartlineBot/0.1 (+https://startline.vip/bot; company-card indexing)"
PROFILE_RE = re.compile(r"https?://(?:www\.)?pitchbook\.com/profiles/"
                        r"(?:company|investor)/[\w.-]+", re.I)

# "Contact Information Website shopvcf.com Ownership Status ..." — the label is
# followed by a bare host, not a link.
_WEBSITE = re.compile(
    r"Website\s+((?:[a-z0-9-]+\.)+[a-z]{2,12})\b", re.I)

# Read only from the contact block, never from the whole page. These labels
# also appear in the profile's table of contents, where they are headings with
# no value after them -- an unanchored search finds those first and yields
# nonsense like "Financing Status: Location". Industry and ownership are left
# to the FAQ, which states them in full sentences.
_CONTACT_ANCHOR = "Contact Information"
_CONTACT_WINDOW = 400
_SUMMARY_FIELDS = (
    ("Ownership Status",
     r"Ownership Status\s+([A-Za-z][A-Za-z0-9 ()/,'-]{2,60}?)\s*(?=Financing Status|Website|Primary Industry|$)"),
    ("Financing Status",
     r"Financing Status\s+([A-Za-z][A-Za-z0-9 ()/,'-]{2,60}?)\s*(?=Ownership Status|Website|Primary Industry|Corporate|$)"),
)


def _host(value: str) -> str:
    parsed = urlparse(value if "://" in (value or "") else "https://" + (value or ""))
    return (parsed.hostname or "").lower().removeprefix("www.")


class PitchBookAdapter(SourceAdapter):
    source_type = "pitchbook"
    # Nothing to fetch until a profile URL has been found for the company.
    deferred_until_discovery = True

    def __init__(self, hasdata_key: str | None = None,
                 agent_db_path: str | None = None):
        self.agent_db_path = agent_db_path
        key = hasdata_key or os.environ.get("HASDATA_API_KEY", "")
        self.search = HasDataClient(key) if key else None
        self.failures: list[dict[str, str]] = []
        self._robots = None

    # -- resolution ----------------------------------------------------------

    def find_profile(self, company_name: str, domain: str | None) -> str | None:
        """Locate this company's PitchBook profile.

        The local index is consulted first. It only knows profiles that have
        already been read, but every read teaches it one more — so a company
        seen before resolves instantly and for free, and a domain match is
        stronger evidence than anything a search result can offer.
        """
        index = self._index()
        if index is not None:
            hit = (index.lookup_by_domain(domain) if domain else None) \
                or index.lookup(company_name)
            if hit:
                return hit["url"]
        if self.search is None:
            return None
        queries = [f'site:pitchbook.com/profiles "{company_name}"']
        if domain:
            queries.append(f'site:pitchbook.com/profiles "{domain}"')
        for query in queries:
            try:
                result = self.search.serp_search(query, num_results=5)
            except Exception:                    # noqa: BLE001
                continue
            organic = (result.get("organicResults")
                       or result.get("organic_results") or [])
            for item in organic:
                link = item.get("link") or item.get("url") or ""
                match = PROFILE_RE.match(link.split("?")[0])
                if not match:
                    continue
                url = match.group(0)
                # When the sitemap index has been built, confirm the URL is one
                # PitchBook actually lists. A search result can be a stale or
                # rewritten link, and a browser fetch is expensive.
                index = self._index()
                if index is not None and index.counts() and not index.knows(url):
                    continue
                return url
        return None

    def _index(self):
        try:
            from .pitchbook_sitemap import PitchBookSitemapIndex
            return PitchBookSitemapIndex()
        except Exception:                        # noqa: BLE001
            return None

    def remember(self, url: str, name: str, domain: str) -> None:
        """Record what a numeric profile reference turned out to be."""
        match = PROFILE_RE.match((url or "").split("?")[0])
        index = self._index()
        if match is None or index is None:
            return
        parts = match.group(0).split("/")
        try:
            index.record_profile(parts[-2], parts[-1], name, domain)
        except Exception:                        # noqa: BLE001
            pass                                 # never fail a fetch over bookkeeping

    def has_sources(self, domain: str | None) -> bool:
        # Answering this truthfully would cost a search per call, and the
        # planner asks repeatedly. Report availability of the mechanism; fetch
        # resolves for real and returns nothing when there is no profile.
        return self.search is not None

    # -- politeness ----------------------------------------------------------

    def _allowed_by_robots(self, url: str) -> bool:
        if self._robots is None:
            parser = urllib.robotparser.RobotFileParser()
            parser.set_url(ROBOTS_URL)
            try:
                parser.read()
            except Exception:                    # noqa: BLE001
                return False
            self._robots = parser
        try:
            return self._robots.can_fetch(USER_AGENT, url)
        except Exception:                        # noqa: BLE001
            return False

    # -- parsing -------------------------------------------------------------

    @staticmethod
    def _json_ld(html: str) -> tuple[dict, dict, list[tuple[str, str]]]:
        """Return (Corporation, PostalAddress, [(question, answer), ...])."""
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html or "", "lxml")
        corporation: dict = {}
        address: dict = {}
        faqs: list[tuple[str, str]] = []
        for tag in soup.find_all("script", type=lambda t: t and "ld+json" in t):
            try:
                data = json.loads(tag.string or tag.get_text() or "")
            except (ValueError, TypeError):
                continue
            for node in (data if isinstance(data, list) else [data]):
                if not isinstance(node, dict):
                    continue
                kind = str(node.get("@type") or "").lower()
                if kind in ("corporation", "organization") and not corporation:
                    corporation = node
                elif kind == "postaladdress" and not address:
                    address = node
                elif kind == "faqpage":
                    for item in node.get("mainEntity") or []:
                        if not isinstance(item, dict):
                            continue
                        question = str(item.get("name") or "").strip()
                        raw = str((item.get("acceptedAnswer") or {}).get("text") or "")
                        # Answers embed markup: competitor links and the empty
                        # <i></i> pairs PitchBook uses to mask paid figures.
                        answer = re.sub(r"<[^>]+>", " ", raw)
                        answer = re.sub(r"\s+", " ", answer).strip()
                        if question and answer:
                            faqs.append((question, answer))
        return corporation, address, faqs

    @staticmethod
    def _visible_text(html: str) -> str:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html or "", "lxml")
        for tag in soup(["script", "style", "noscript", "svg", "nav", "header"]):
            tag.decompose()
        return re.sub(r"\s+", " ", soup.get_text(" ", strip=True))

    @classmethod
    def _stated_website(cls, text: str) -> str:
        match = _WEBSITE.search(text)
        return _host(match.group(1)) if match else ""

    # -- document ------------------------------------------------------------

    def _document(self, url: str, html: str, domain: str | None) -> ScrapedDoc | None:
        corporation, address, faqs = self._json_ld(html)
        text = self._visible_text(html)
        if not corporation and not faqs:
            self.failures.append({"source_url": url,
                                  "reason": "no structured company data on page"})
            return None

        stated = self._stated_website(text)
        if not domain:
            self.failures.append({"source_url": url,
                                  "reason": "no company domain to verify against"})
            return None
        if not stated:
            self.failures.append({"source_url": url,
                                  "reason": "profile states no website; identity unverifiable"})
            return None
        if stated != _host(domain):
            self.failures.append({
                "source_url": url,
                "reason": f"identity mismatch: profile states {stated}, expected {_host(domain)}"})
            return None

        name = corporation.get("name") or ""
        # Teach the index what this numeric reference actually is, so the next
        # lookup of this company needs no search and no browser fetch.
        self.remember(url, name, stated)
        lines = [f"PAGE URL: {url}",
                 f"PAGE TITLE: {name} - PitchBook profile", ""]

        lines.append("--- STRUCTURED DATA (declared by this page's markup) ---")
        lines.append(f"schema.org url: https://{stated}")
        for key, label in (("name", "schema.org name"),
                           ("description", "schema.org description"),
                           ("foundingDate", "schema.org founding date"),
                           ("numberOfEmployees", "schema.org employees"),
                           ("logo", "schema.org logo")):
            value = corporation.get(key)
            if isinstance(value, (str, int)) and str(value).strip():
                lines.append(f"{label}: {str(value).strip()}")
        for social in corporation.get("sameAs") or []:
            if isinstance(social, str) and social.startswith("http"):
                platform = _host(social).split(".")[0].title()
                lines.append(f"SOCIAL PROFILE: {platform} | {social}")
        if address:
            joined = ", ".join(
                str(address[k]) for k in ("streetAddress", "addressLocality",
                                          "addressRegion", "postalCode")
                if address.get(k))
            if joined:
                lines.append(f"schema.org address: {joined}")

        anchor = text.find(_CONTACT_ANCHOR)
        window = text[anchor:anchor + _CONTACT_WINDOW] if anchor >= 0 else ""
        summary = []
        for label, pattern in _SUMMARY_FIELDS:
            match = re.search(pattern, window)
            if match:
                summary.append(f"{label}: {match.group(1).strip()}")
        if summary:
            lines += ["", "--- PROFILE SUMMARY (labelled figures on the page) ---"]
            lines += summary

        if faqs:
            lines += ["", "--- PUBLISHED COMPANY FACTS (PitchBook Q&A) ---"]
            for question, answer in faqs:
                lines.append(f"{question} {answer}")
                if re.search(r"raise|funding", question, re.I):
                    amount = parse_money(answer)
                    if amount:
                        lines.append("   parsed total funding (USD, from the answer "
                                     f"above): {int(amount)}")

        content = "\n".join(lines)
        return ScrapedDoc(raw_content=content, source_url=url,
                          original_chars=len(content), content_truncated=False)

    # -- entry point ---------------------------------------------------------

    def fetch(self, company_name: str, domain: str | None, hints=None,
              company_id: Optional[str] = None) -> list[ScrapedDoc]:
        self.failures = []
        from .cbinsights_adapter import pinned_profile
        # Directory-origin companies were created from a known listing.
        url = pinned_profile(self.agent_db_path, company_id, self.source_type)
        if not url:
            url = self.find_profile(company_name, domain)
        if not url:
            return []
        if not self._allowed_by_robots(url):
            self.failures.append({"source_url": url,
                                  "reason": "disallowed by robots.txt for this crawler"})
            return []
        try:
            result = scrape_ladder.retrieve_url(url)
        except Exception as exc:                              # noqa: BLE001
            self.failures.append({"source_url": url, "reason": str(exc)[:200]})
            return []
        if not result.pages:
            reason = next((a.error or a.skipped for a in reversed(result.attempts)
                           if a.error or a.skipped), "no usable content")
            self.failures.append({"source_url": url, "reason": reason})
            return []
        doc = self._document(url, result.pages[0].html, domain)
        return [doc] if doc else []
