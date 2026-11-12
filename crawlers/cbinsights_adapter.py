"""CB Insights company profiles as a pipeline source.

Fetched with the console's own crawl ladder (Scrapy -> Crawlee -> Playwright)
under the `StartlineBot/0.1` user-agent, which falls under `User-agent: *` in
cbinsights.com/robots.txt where `/company/` is allowed. The named AI-training
crawlers their robots.txt disallows (GPTBot, ClaudeBot, CCBot, PerplexityBot,
Google-Extended) are operated by other parties and are not involved here;
`_allowed_by_robots` re-checks the live policy for the UA we actually send
before every fetch, so if that policy changes the adapter stops on its own.

Why this source is worth having: a CB Insights profile publishes two
schema.org blocks that state, in quotable prose, the things a company website
almost never does --

    Corporation : name, url, description, foundingDate, logo
    FAQPage     : "X's latest funding round is Series B."
                  "X raised a total of $106.8M."
                  "Investors of X include A, B, C and 8 more."
                  "X's headquarters is located at ..."

The document handed downstream is built from those blocks alone, not from the
rendered page. A CB Insights page is ~15,000 characters of which the great
majority is their own site navigation; extracting from that would cost about
ten times the tokens and invite the model to read menu items as facts.
Monetary totals are additionally parsed to a number here, because the
extraction prompt permits only Korean unit expansion and would otherwise
return `106.8` for "$106.8M" -- the parsed line sits beside the verbatim one,
so both value and evidence remain literally present in the source.
"""
from __future__ import annotations

from typing import Optional

import json
import re
import urllib.robotparser
from urllib.parse import urlparse

import startline_agent  # noqa: F401  (puts the agent modules on sys.path)
from orchestrator import ScrapedDoc, SourceAdapter

from . import scrape_ladder
from .cbinsights_sitemap import CBInsightsSitemapIndex, entity_url

ROBOTS_URL = "https://www.cbinsights.com/robots.txt"
USER_AGENT = "StartlineBot/0.1 (+https://startline.vip/bot; company-card indexing)"

_MULTIPLIER = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000, "t": 1_000_000_000_000}
_MONEY = re.compile(r"\$\s?([\d,]+(?:\.\d+)?)\s*([KMBT])?\b", re.I)


def _host(value: str) -> str:
    parsed = urlparse(value if "://" in (value or "") else "https://" + (value or ""))
    return (parsed.hostname or "").lower().removeprefix("www.")


def parse_money(text: str) -> float | None:
    """'$106.8M' -> 106800000.0. Returns None when no amount is stated."""
    match = _MONEY.search(text or "")
    if not match:
        return None
    try:
        amount = float(match.group(1).replace(",", ""))
    except ValueError:
        return None
    return amount * _MULTIPLIER.get((match.group(2) or "").lower(), 1)


def pinned_profile(db_path: str, company_id: Optional[str],
                   source: str) -> Optional[str]:
    """The profile URL a directory-origin company was created from.

    Returns None for zone-file companies, which have no pinned listing and
    must resolve a profile by name or domain as before.
    """
    if not company_id:
        return None
    import sqlite3
    try:
        db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        row = db.execute(
            "SELECT url FROM directory_profile WHERE company_id=? AND source=?",
            (company_id, source)).fetchone()
        db.close()
    except sqlite3.Error:
        return None
    return row[0] if row else None


class CBInsightsAdapter(SourceAdapter):
    source_type = "cbinsights"
    # Only worth enqueueing once a slug for this company is known.
    deferred_until_discovery = True

    def __init__(self, db_path: str | None = None,
                 agent_db_path: str | None = None):
        self.index = CBInsightsSitemapIndex(db_path)
        # The agent's own database, where directory_profile pins live. Separate
        # file from the sitemap index, so it has to be handed in.
        self.agent_db_path = agent_db_path
        self.failures: list[dict[str, str]] = []
        self._robots = None

    # -- discovery -----------------------------------------------------------

    def _slug(self, company_name: str, domain: str | None) -> str | None:
        for candidate in (company_name, (domain or "").split(".")[0]):
            if not candidate:
                continue
            hit = self.index.lookup(candidate, "company")
            if hit:
                return hit["slug"]
        return None

    def has_sources(self, domain: str | None) -> bool:
        return bool(domain and self.index.lookup(domain.split(".")[0], "company"))

    # -- politeness ----------------------------------------------------------

    def _allowed_by_robots(self, url: str) -> bool:
        """Re-check the live robots policy for the UA this console sends."""
        if self._robots is None:
            parser = urllib.robotparser.RobotFileParser()
            parser.set_url(ROBOTS_URL)
            try:
                parser.read()
            except Exception:                    # noqa: BLE001
                return False                     # unreadable policy -> do not fetch
            self._robots = parser
        try:
            return self._robots.can_fetch(USER_AGENT, url)
        except Exception:                        # noqa: BLE001
            return False

    # -- extraction of the structured blocks ---------------------------------

    @staticmethod
    def _blocks(html: str) -> tuple[dict, list[tuple[str, str]]]:
        """Return (Corporation node, [(question, answer), ...])."""
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html or "", "lxml")
        corporation: dict = {}
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
                if kind in ("corporation", "organization", "company") and not corporation:
                    corporation = node
                elif kind == "faqpage":
                    for item in node.get("mainEntity") or []:
                        if not isinstance(item, dict):
                            continue
                        question = str(item.get("name") or "").strip()
                        answer = str((item.get("acceptedAnswer") or {}).get("text") or "").strip()
                        if question and answer:
                            faqs.append((question, answer))
        return corporation, faqs

    # Labelled figures the profile renders outside the schema.org blocks. The
    # page states these as "<label> <value>" runs in its summary strip, and
    # they carry the funding facts the JSON-LD omits.
    _SUMMARY_FIELDS = (
        ("Founded Year", r"Founded Year\s+((?:18|19|20)\d{2})"),
        # The page writes this as "Stage Acquired | Acquired" — status and
        # round separated by a pipe, so the pipe belongs inside the value.
        ("Stage", r"Stage\s+([A-Za-z][A-Za-z0-9 /&'|.-]{2,60}?)\s+(?=Total Raised|Valuation|Revenue|Employees|About|Headquarters)"),
        ("Total Raised", r"Total Raised\s+(\$[\d,.]+[KMBT]?)"),
        ("Valuation", r"Valuation\s+(\$[\d,.]+[KMBT]?)"),
        ("Revenue", r"Revenue\s+(\$[\d,.]+[KMBT]?)"),
        ("Employees", r"Employees\s+([\d,]+)"),
    )

    @staticmethod
    def _visible_text(html: str) -> str:
        """Rendered text from the company section only.

        Everything before the summary strip is CB Insights' own site
        navigation — roughly 11,000 of the page's 15,000 characters, and it
        contains headings ("... Headquarters Locations Technology Vendors")
        that otherwise match the section anchors below and drag a menu into
        the evidence document.
        """
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html or "", "lxml")
        for tag in soup(["script", "style", "noscript", "svg", "nav", "header"]):
            tag.decompose()
        text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))
        anchor = re.search(r"Founded Year\s+(?:18|19|20)\d{2}", text)
        return text[anchor.start():] if anchor else text

    def _summary(self, text: str) -> list[str]:
        """Pull the labelled summary figures out of the rendered page."""
        out = []
        for label, pattern in self._SUMMARY_FIELDS:
            match = re.search(pattern, text)
            if not match:
                continue
            value = match.group(1).strip()
            # CB Insights masks paid figures as "$0000" / "$00.0M".
            if re.fullmatch(r"\$0[0.,]*[KMBT]?", value):
                continue
            out.append(f"{label}: {value}")
        return out

    @staticmethod
    def _section(text: str, start: str, stop: tuple[str, ...]) -> str | None:
        """Text between a heading and the next known heading."""
        begin = text.find(start)
        if begin < 0:
            return None
        begin += len(start)
        end = len(text)
        for marker in stop:
            found = text.find(marker, begin)
            if 0 <= found < end:
                end = found
        body = text[begin:end].strip(" :-")
        return body if len(body) > 25 else None

    def _document(self, url: str, html: str, domain: str | None) -> ScrapedDoc | None:
        corporation, faqs = self._blocks(html)
        if not corporation and not faqs:
            self.failures.append({"source_url": url,
                                  "reason": "no structured company data on page"})
            return None

        # Identity gate. The profile states the company's own website; if that
        # disagrees with the domain we are enriching, this is a different
        # company with a similar name and nothing here may be asserted.
        stated = _host(corporation.get("url") or "")
        if domain and stated and stated != _host(domain):
            self.failures.append({
                "source_url": url,
                "reason": f"identity mismatch: profile states {stated}, expected {_host(domain)}"})
            return None

        lines = [f"PAGE URL: {url}",
                 f"PAGE TITLE: {corporation.get('name') or ''} - CB Insights profile", ""]

        if corporation:
            lines.append("--- STRUCTURED DATA (declared by this page's markup) ---")
            for key, label in (("name", "schema.org name"),
                               ("description", "schema.org description"),
                               ("disambiguatingDescription", "schema.org description"),
                               ("foundingDate", "schema.org founding date"),
                               ("url", "schema.org url"),
                               ("logo", "schema.org logo")):
                value = corporation.get(key)
                if isinstance(value, str) and value.strip():
                    lines.append(f"{label}: {value.strip()}")

        if faqs:
            lines += ["", "--- PUBLISHED COMPANY FACTS (CB Insights Q&A) ---"]
            for question, answer in faqs:
                lines.append(f"{question} {answer}")
                # "raised a total of $106.8M" -> a value the schema can hold.
                if re.search(r"raise|funding|total", question, re.I):
                    amount = parse_money(answer)
                    if amount:
                        lines.append(
                            f"   parsed total funding (USD, from the answer above): "
                            f"{int(amount)}")

        # Everything above comes from markup. The rest of the profile is
        # rendered prose, and it carries what the markup leaves out: the
        # funding stage, the full address and phone, and the long description.
        text = self._visible_text(html)
        summary = self._summary(text)
        if summary:
            lines += ["", "--- PROFILE SUMMARY (labelled figures on the page) ---"]
            lines += summary
            for item in summary:
                if item.startswith("Total Raised:"):
                    amount = parse_money(item)
                    if amount:
                        lines.append(f"   parsed total raised (USD): {int(amount)}")

        about = self._section(
            text, f"About {corporation.get('name') or ''}".strip(),
            ("Headquarters Location", "Expert Collections", "Latest ", "Loading..."))
        if about:
            lines += ["", "--- ABOUT (as published on the profile) ---", about[:2500]]

        # "Location" singular: the plural is a navigation heading.
        location = self._section(
            text, "Headquarters Location ",
            ("Loading...", "Expert Collections", "Latest ", "Suggested "))
        if location:
            lines += ["", "--- HEADQUARTERS (as published on the profile) ---", location[:400]]

        content = "\n".join(lines)
        return ScrapedDoc(raw_content=content, source_url=url,
                          original_chars=len(content), content_truncated=False)

    # -- entry point ---------------------------------------------------------

    def fetch(self, company_name: str, domain: str | None, hints=None,
              company_id: Optional[str] = None) -> list[ScrapedDoc]:
        self.failures = []
        # A directory-origin company was created from a specific listing, so
        # read that page rather than trying to resolve one by name.
        url = pinned_profile(self.agent_db_path, company_id, self.source_type)
        if not url:
            slug = self._slug(company_name, domain)
            if not slug:
                return []                  # not in the index; nothing to fetch
            url = entity_url("company", slug)
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
