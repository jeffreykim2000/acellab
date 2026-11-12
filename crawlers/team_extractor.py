"""TeamMemberExtractor — finds and validates team member data from website + LinkedIn."""
import re
import logging
from typing import Dict, List, Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

LINKEDIN_RE = re.compile(r"https?://(?:www\.)?linkedin\.com/in/[\w\-]+/?", re.IGNORECASE)
EMAIL_RE    = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")

# Maps regex patterns to canonical title strings so "Founder/CEO" == "CEO"
TITLE_MAP = [
    (r"chief\s+executive|(?<!\w)ceo(?!\w)", "CEO"),
    (r"chief\s+technology|(?<!\w)cto(?!\w)", "CTO"),
    (r"chief\s+operating|(?<!\w)coo(?!\w)", "COO"),
    (r"chief\s+financial|(?<!\w)cfo(?!\w)", "CFO"),
    (r"chief\s+marketing|(?<!\w)cmo(?!\w)", "CMO"),
    (r"chief\s+product|(?<!\w)cpo(?!\w)", "CPO"),
    (r"founder\s*/\s*ceo|founder\s+&\s+ceo", "Founder & CEO"),
    (r"vp\s+engineering|vice\s+president.*engineering", "VP Engineering"),
    (r"vp\s+sales|vice\s+president.*sales", "VP Sales"),
    (r"head\s+of\s+product", "Head of Product"),
    (r"head\s+of\s+design", "Head of Design"),
]

# Team page paths to probe in order
TEAM_PATHS = ["/team", "/about", "/about-us", "/leadership", "/founders", "/people", "/company/team"]


def _normalise_title(raw: str) -> str:
    if not raw:
        return raw
    for pattern, canonical in TITLE_MAP:
        if re.search(pattern, raw, re.IGNORECASE):
            return canonical
    return raw.strip()


class TeamMember:
    def __init__(
        self,
        name: str = "",
        role: str = "",
        email: str = "",
        phone: str = "",
        linkedin_url: str = "",
        source: str = "",
        confidence: str = "medium",
    ):
        self.name = name
        self.role = role
        self.email = email
        self.phone = phone
        self.linkedin_url = linkedin_url
        self.source = source
        self.confidence = confidence
        self.issues: List[str] = []

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "role": self.role,
            "email": self.email,
            "phone": self.phone,
            "linkedin_url": self.linkedin_url,
            "source": self.source,
            "confidence": self.confidence,
            "issues": self.issues,
        }


class TeamMemberExtractor:
    """
    Two-stage team extraction:
    1. Website pages via Firecrawl (markdown parsing)
    2. LinkedIn people search via Apify (structured data)
    Results are deduplicated and merged.
    """

    def __init__(self, apify_client=None, firecrawl_client=None):
        self._apify = apify_client
        self._firecrawl = firecrawl_client

    # ------------------------------------------------------------------ #
    #  Markdown parsing                                                    #
    # ------------------------------------------------------------------ #

    # Patterns that signal a person's name in markdown
    _NAME_PATTERNS = [
        # **Bold Name** (most common in Firecrawl output)
        re.compile(r"^\*\*([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,3})\*\*\s*$"),
        # ### Name  or  ## Name (heading-based team card)
        re.compile(r"^#{2,4}\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,3})\s*$"),
        # Name | Role  (pipe-separated)
        re.compile(r"^([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,3})\s*\|\s*(.+)$"),
        # **Name** — Role  (inline role)
        re.compile(r"^\*\*([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,3})\*\*\s*[—\-–]\s*(.+)$"),
    ]

    def _parse_markdown(self, markdown: str, source_url: str) -> List[TeamMember]:
        """
        Multi-pattern heuristic parser for team member blocks.
        Handles: **Bold Name**, ## Name, Name | Role, and inline dash formats.
        """
        members: List[TeamMember] = []
        lines = markdown.split("\n")
        seen_names: set = set()

        for i, line in enumerate(lines):
            raw = line.strip()
            if not raw:
                continue

            name = role = email = linkedin = ""

            for pat in self._NAME_PATTERNS:
                m = pat.match(raw)
                if not m:
                    continue
                name = m.group(1)
                if len(m.groups()) >= 2 and m.group(2):
                    role = _normalise_title(m.group(2).strip())
                break

            if not name:
                continue
            if name.lower() in seen_names:
                continue
            seen_names.add(name.lower())

            # Read the next few lines for role, email, LinkedIn
            for j in range(i + 1, min(i + 6, len(lines))):
                nxt = lines[j].strip()
                if not nxt:
                    continue
                # Stop if we hit another name-like line
                if any(p.match(nxt) for p in self._NAME_PATTERNS):
                    break
                # Role: short non-url, non-email line
                if (not role and nxt and not nxt.startswith("#") and not nxt.startswith("!")
                        and len(nxt) < 120 and "@" not in nxt and "http" not in nxt):
                    role = _normalise_title(nxt)
                em = EMAIL_RE.search(nxt)
                if em and not email:
                    email = em.group(0)
                li = LINKEDIN_RE.search(nxt)
                if li and not linkedin:
                    linkedin = li.group(0)

            member = TeamMember(
                name=name, role=role, email=email,
                linkedin_url=linkedin, source="website",
            )
            if not role:
                member.issues.append("Missing role")
            if not email and not linkedin:
                member.issues.append("No contact info")

            member.confidence = (
                "high"   if role and (email or linkedin) else
                "medium" if role else
                "low"
            )
            members.append(member)

        return members

    # ------------------------------------------------------------------ #
    #  LinkedIn URL validation                                             #
    # ------------------------------------------------------------------ #

    def _validate_linkedin_url(self, url: str, person_name: str) -> bool:
        """
        Check that the URL slug contains at least one name token.
        e.g. linkedin.com/in/john-smith should match "John Smith".
        """
        if not url or not LINKEDIN_RE.match(url):
            return False
        slug = url.rstrip("/").split("/")[-1].lower()
        name_tokens = [t for t in person_name.lower().split() if len(t) > 2]
        return any(tok in slug for tok in name_tokens)

    # ------------------------------------------------------------------ #
    #  Website extraction via Firecrawl                                   #
    # ------------------------------------------------------------------ #

    def extract_from_website(self, url: str, log_cb=None) -> List[TeamMember]:
        def _log(msg):
            logger.info(msg)
            if log_cb:
                try:
                    log_cb(msg)
                except Exception:
                    pass

        if not self._firecrawl or not url:
            return []

        try:
            parsed = urlparse(url)
            base = f"{parsed.scheme}://{parsed.netloc}"
        except Exception:
            return []

        for path in TEAM_PATHS:
            try:
                _log(f"  Checking {base}{path}")
                result = self._firecrawl.scrape_url(base + path, formats=["markdown"])
                md = (result.get("data") or {}).get("markdown") or result.get("markdown", "")
                if not md or len(md) < 200:
                    continue
                members = self._parse_markdown(md, base + path)
                if members:
                    _log(f"  Found {len(members)} team members at {base}{path}")
                    return members
            except Exception as e:
                logger.debug("Team page %s%s failed: %s", base, path, e)

        return []

    # ------------------------------------------------------------------ #
    #  LinkedIn people search via Apify                                   #
    # ------------------------------------------------------------------ #

    def extract_from_linkedin(self, company_name: str) -> List[TeamMember]:
        if not self._apify:
            return []
        try:
            items = self._apify.run_actor("2SyF0bVxmgGr8IVCZ", {
                "searchType": "people",
                "query": f"{company_name} leadership",
                "maxResults": 20,
            })
            members = []
            for item in (items or []):
                name    = item.get("fullName") or item.get("name", "")
                role    = _normalise_title(item.get("headline") or item.get("title", ""))
                li_url  = item.get("linkedInUrl") or item.get("url", "")
                valid   = self._validate_linkedin_url(li_url, name) if name else False

                m = TeamMember(
                    name=name, role=role,
                    linkedin_url=li_url if valid else "",
                    source="linkedin",
                    confidence="high" if (name and role and valid) else "medium",
                )
                if li_url and not valid:
                    m.issues.append("LinkedIn URL may not match person")
                if name:
                    members.append(m)
            return members
        except Exception as e:
            err = str(e)
            if "403" in err and "full-permission" in err:
                logger.warning(
                    "LinkedIn actor needs permission approval — visit "
                    "https://console.apify.com/actors/2SyF0bVxmgGr8IVCZ?approvePermissions=true"
                )
            else:
                logger.error("LinkedIn team extraction failed: %s", e)
            return []

    # ------------------------------------------------------------------ #
    #  Deduplication                                                       #
    # ------------------------------------------------------------------ #

    def _deduplicate(self, members: List[TeamMember]) -> List[TeamMember]:
        """Merge duplicates by name. Fill in missing fields from the duplicate."""
        seen: Dict[str, TeamMember] = {}
        for m in members:
            key = m.name.lower().strip()
            if not key:
                continue
            if key not in seen:
                seen[key] = m
            else:
                existing = seen[key]
                if not existing.email and m.email:
                    existing.email = m.email
                if not existing.linkedin_url and m.linkedin_url:
                    existing.linkedin_url = m.linkedin_url
                if not existing.role and m.role:
                    existing.role = m.role
                # Upgrade confidence if we now have more info
                if existing.role and (existing.email or existing.linkedin_url):
                    existing.confidence = "high"
        return list(seen.values())

    # ------------------------------------------------------------------ #
    #  Public entry point                                                  #
    # ------------------------------------------------------------------ #

    def extract(self, company_name: str, url: str = "") -> List[Dict]:
        """
        Extract all team members. Tries website first, then LinkedIn.
        Returns deduplicated list of dicts with confidence scores.
        """
        website = self.extract_from_website(url) if url else []
        linkedin = self.extract_from_linkedin(company_name)
        combined = self._deduplicate(website + linkedin)

        if not combined:
            logger.info("No team members found for %r", company_name)

        return [m.to_dict() for m in combined]
