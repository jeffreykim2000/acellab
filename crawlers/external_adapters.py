"""Structured external company sources wired into the Agent OS fact store."""
from __future__ import annotations

from typing import Optional

import os
import re
from urllib.parse import urlparse

from .apify_client import ApifyClient
from .crunchbase_actor_test import normalise
from .crunchbase_slug_resolver import CrunchbaseSlugResolver
from .directory_news_scraper import DirectoryAndNewsScraper
from .firecrawl_client import FirecrawlClient
from .hasdata_client import HasDataClient

import startline_agent  # noqa: F401 - exposes agent modules on sys.path
from orchestrator import DirectFact, ScrapedDoc, SourceAdapter, StructuredAdapter


def _host(value: str) -> str:
    parsed = urlparse(value if "://" in (value or "") else "https://" + (value or ""))
    return (parsed.hostname or "").lower().removeprefix("www.")


# A person holding one of these titles is the company's chief executive. Used
# to fill ceo_name, which the "Team Member Name" manifest row requires
# alongside key_people (all_of), so a team list without it leaves the row red.
CEO_TITLE = re.compile(r"\b(ceo|chief executive|founder|co-?founder|대표이사|대표)\b", re.I)


def _people(raw_people) -> list[dict]:
    """Normalise an actor's people list into schema `team_members` shape."""
    members: list[dict] = []
    seen: set[str] = set()
    for raw in raw_people or []:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name") or raw.get("fullName") or "").strip()
        if len(name) < 2 or name.casefold() in seen:
            continue
        seen.add(name.casefold())
        member = {"name": name}
        role = str(raw.get("title") or raw.get("role") or raw.get("position")
                   or raw.get("jobTitle") or "").strip()
        if len(role) > 1:
            member["role"] = role
        profile = str(raw.get("linkedin_url") or raw.get("profileUrl")
                      or raw.get("url") or "").strip()
        if profile.startswith(("http://", "https://")) and "linkedin.com/" in profile.lower():
            member["linkedin_url"] = profile
        photo = str(raw.get("photo_url") or raw.get("photoUrl")
                    or raw.get("imageUrl") or "").strip()
        if photo.startswith(("http://", "https://")):
            member["photo_url"] = photo
        members.append(member)
    return members


def _rounds(raw_rounds) -> list[dict]:
    """Normalise funding rounds into schema `funding_rounds` shape.

    Sorted newest-first because `compatibility_projections` treats element 0 as
    the latest round when deriving funding_date and funding_stage.
    """
    rounds: list[dict] = []
    for raw in raw_rounds or []:
        if not isinstance(raw, dict):
            continue
        entry: dict = {}
        date = str(raw.get("date") or raw.get("announced_on") or "").strip()
        if date:
            entry["date"] = date
        stage = str(raw.get("type") or raw.get("stage")
                    or raw.get("investment_type") or "").strip()
        if len(stage) > 1:
            entry["stage"] = stage
        amount = raw.get("amount")
        if isinstance(amount, (int, float)) and not isinstance(amount, bool) and amount > 0:
            entry["amount"] = amount
            entry["currency"] = "USD"      # normalise() resolves amounts to USD
        leads = [str(item).strip() for item in (raw.get("lead_investors") or [])
                 if str(item or "").strip()]
        if leads:
            entry["lead_investors"] = leads
        # The registry validator requires at least one of these three.
        if entry.get("date") or entry.get("stage") or entry.get("amount"):
            rounds.append(entry)
    rounds.sort(key=lambda item: str(item.get("date") or ""), reverse=True)
    return rounds


def _ceo(members: list[dict]) -> str | None:
    return next((m["name"] for m in members
                 if CEO_TITLE.search(m.get("role", ""))), None)


class CrunchbaseAdapter(StructuredAdapter):
    source_type = "crunchbase"

    def __init__(self, token: str | None = None):
        self.client = ApifyClient(token or os.environ.get("APIFY_API_TOKEN", ""))
        self.resolver = CrunchbaseSlugResolver(apify_client=self.client)

    def fetch_facts(self, company_name: str, domain: str | None,
                    company_id: Optional[str] = None) -> list[DirectFact]:
        resolution = self.resolver.resolve_slug(
            company_name, {"website_url": f"https://{domain}" if domain else ""})
        if not resolution.slug or resolution.confidence < 0.7:
            return []
        source_url = resolution.crunchbase_url
        items = self.client.run_actor(
            "pratikdani~crunchbase-companies-bulk-scraper-no-cookies",
            {"urls": [source_url]},
        )
        if not items:
            return []
        data = normalise(items[0])
        if domain and _host(data.get("website") or "") != _host(domain):
            return []

        mapping = {
            "name": "brand_name", "description": "description",
            "founded_year": "founded_year", "hq_location": "hq_location",
            "employee_count": "employee_count", "industry": "industry",
            "email": "contact_email", "phone": "contact_phone",
            "total_funding": "funding_total_usd",
        }
        facts = []
        for source_field, field in mapping.items():
            value = data.get(source_field)
            if value not in (None, "", [], {}):
                facts.append(DirectFact(field, value,
                                        f"Crunchbase {source_field}={value!r}", source_url))

        # Crunchbase is the canonical public record for funding history, and
        # normalise() already parses funding_rounds_list into per-round dicts.
        # The orchestrator projects these into funding_date / funding_stage /
        # lead_investors, which is what the manifest rows are backed by.
        rounds = _rounds(data.get("funding_rounds"))
        if rounds:
            facts.append(DirectFact(
                "funding_rounds", rounds,
                f"Crunchbase funding_rounds_list ({len(rounds)} rounds)", source_url))

        # current_employees_image_list → team_members, which projects into
        # key_people / team_photos / team_linkedin_urls.
        members = _people(data.get("team_members"))
        if members:
            facts.append(DirectFact(
                "team_members", members,
                f"Crunchbase current employees ({len(members)} people)", source_url))
            ceo = _ceo(members)
            if ceo:
                facts.append(DirectFact(
                    "ceo_name", ceo, f"Crunchbase employee title match: {ceo}", source_url))
        return facts


SIZE_BUCKETS = (
    (10, "1-10"), (50, "11-50"), (200, "51-200"), (1000, "201-1000"),
)


def _size_bucket(count: int) -> str:
    return next((label for ceiling, label in SIZE_BUCKETS if count <= ceiling), "1000+")


def _headcount(value) -> int | None:
    """Parse LinkedIn's '51-200 employees' / '1,001+' size strings to a midpoint."""
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    numbers = [int(n.replace(",", "")) for n in re.findall(r"\d[\d,]*", str(value or ""))]
    if not numbers:
        return None
    # A range states both ends; the schema asks for the midpoint.
    return round(sum(numbers[:2]) / 2) if len(numbers) >= 2 else numbers[0]


class LinkedInAdapter(StructuredAdapter):
    source_type = "linkedin"

    def __init__(self, hasdata_key: str | None = None,
                 firecrawl_key: str | None = None,
                 apify_token: str | None = None):
        hasdata = HasDataClient(hasdata_key or os.environ.get("HASDATA_API_KEY", ""))
        firecrawl = FirecrawlClient(firecrawl_key or os.environ.get("FIRECRAWL_API_KEY", ""))
        self.collector = DirectoryAndNewsScraper(hasdata, firecrawl, None)
        token = apify_token or os.environ.get("APIFY_API_TOKEN", "")
        self.apify = ApifyClient(token) if token else None
        self.actor = os.environ.get("STARTLINE_LINKEDIN_ACTOR",
                                    "2SyF0bVxmgGr8IVCZ")
        # Actor input schemas differ between LinkedIn scrapers and the key is
        # not discoverable without spending a run. Overridable so switching
        # actors does not need a code change.
        self.actor_input_key = os.environ.get(
            "STARTLINE_LINKEDIN_ACTOR_INPUT_KEY", "profileUrls")

    def _apify_people(self, linkedin_url: str) -> list[dict]:
        """Fetch LinkedIn people via the Apify actor. Never raises."""
        if self.apify is None:
            return []
        try:
            items = self.apify.run_actor(
                self.actor, {self.actor_input_key: [linkedin_url]})
        except Exception:      # noqa: BLE001 — a paid source must never fail a job
            return []
        people: list[dict] = []
        for item in items or []:
            if not isinstance(item, dict):
                continue
            # Actors return either a company record carrying a people list, or
            # one row per person. Accept both.
            nested = next((item[key] for key in
                           ("employees", "people", "team", "profiles", "staff")
                           if isinstance(item.get(key), list)), None)
            people.extend(nested if nested is not None else [item])
        return _people(people)

    def fetch_facts(self, company_name: str, domain: str | None,
                    company_id: Optional[str] = None) -> list[DirectFact]:
        linkedin_url = self.collector._find_linkedin_url(company_name)
        if not linkedin_url:
            return []
        data = self.collector._linkedin_company_info(company_name, linkedin_url) or {}
        claimed_domain = _host(data.get("url") or "")
        # Identity gate: a profile claiming a different website is a different
        # company. Only enforced when the profile actually stated one.
        if domain and claimed_domain and claimed_domain != _host(domain):
            return []
        # Without a company record there is nothing tying the actor's people to
        # this company, so people alone are not worth asserting.
        if not data:
            return []

        mapping = {
            "name": "brand_name", "introduction": "description",
            "founded_year": "founded_year", "hq_location": "hq_location",
            "industry": "industry",
        }
        facts = []
        for source_field, field in mapping.items():
            value = data.get(source_field)
            if value not in (None, "", [], {}):
                facts.append(DirectFact(field, value,
                                        f"LinkedIn {source_field}={value!r}", linkedin_url))

        # LinkedIn is the rank-0 source for headcount, but reports it as a
        # range string rather than a number.
        count = _headcount(data.get("size"))
        if count:
            facts.append(DirectFact("employee_count", count,
                                    f"LinkedIn size={data.get('size')!r}", linkedin_url))
            facts.append(DirectFact("company_size_bucket", _size_bucket(count),
                                    f"LinkedIn size={data.get('size')!r}", linkedin_url))

        members = self._apify_people(linkedin_url)
        if members:
            facts.append(DirectFact(
                "team_members", members,
                f"LinkedIn people via Apify actor ({len(members)} people)", linkedin_url))
            ceo = _ceo(members)
            if ceo:
                facts.append(DirectFact(
                    "ceo_name", ceo, f"LinkedIn profile title match: {ceo}", linkedin_url))
        return facts


class NewsAdapter(SourceAdapter):
    source_type = "news"

    def __init__(self, api_key: str | None = None):
        self.client = HasDataClient(api_key or os.environ.get("HASDATA_API_KEY", ""))

    def fetch(self, company_name: str, domain: str | None, hints=None,
              company_id: Optional[str] = None) -> list[ScrapedDoc]:
        result = self.client.serp_search(
            f'"{company_name}" ({domain or "company"}) funding founder headquarters',
            num_results=10)
        organic = result.get("organicResults") or result.get("organic_results") or []
        docs = []
        for item in organic[:8]:
            url = item.get("link") or item.get("url") or ""
            title = item.get("title") or ""
            snippet = item.get("snippet") or item.get("description") or ""
            if url and len(snippet) >= 40:
                docs.append(ScrapedDoc(
                    raw_content=f"ARTICLE TITLE: {title}\nARTICLE URL: {url}\n\n{snippet}",
                    source_url=url))
        return docs
