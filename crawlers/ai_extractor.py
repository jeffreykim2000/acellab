"""ai_extractor.py — Claude API extraction of structured company data from website text."""
import json
import logging
import os
from typing import Dict

logger = logging.getLogger(__name__)

_SYSTEM = (
    "You are a company data extraction assistant. "
    "Extract structured company information from website content. "
    "Return ONLY valid JSON — no prose, no code fences. "
    "Use null for any field not found. "
    "For team_members, extract every person with a named role you can find. "
    "For tags, extract industry keywords, technology areas, and product categories."
)

_SCHEMA = """
{
  "name": "company name (string)",
  "industry": "industry or sector (string)",
  "size": "employee count or range string, e.g. '10-50 employees'",
  "founded_year": 2020,
  "hq_location": "City, Country",
  "address": "full street address if present",
  "email": "primary contact email",
  "phone": "primary phone number",
  "linkedin_url": "https://linkedin.com/company/...",
  "tags": ["keyword1", "keyword2"],
  "introduction": "2-3 sentence company description",
  "team_members": [
    {
      "name": "Full Name",
      "role": "Job Title",
      "email": "email or null",
      "linkedin_url": "linkedin url or null"
    }
  ]
}
"""


def extract_with_claude(
    markdown_text: str,
    company_name: str,
    url: str,
    api_key: str = "",
) -> Dict:
    """
    Send combined website markdown to Claude and extract structured company data.
    Returns a flat dict; empty dict if extraction fails or anthropic is not available.
    """
    api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key or not markdown_text.strip():
        return {}

    try:
        import anthropic as _anthropic
    except ImportError:
        logger.warning("anthropic package not installed — skipping AI extraction. Run: pip install anthropic")
        return {}

    # Truncate to avoid very large context — first 8k chars cover most homepages + about pages
    content = markdown_text[:8000]
    user_msg = (
        f"Company name: {company_name}\n"
        f"Website: {url}\n\n"
        f"Website content:\n{content}\n\n"
        f"Return JSON matching this schema (null for missing fields):\n{_SCHEMA}"
    )

    try:
        client = _anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1500,
            system=_SYSTEM,
            messages=[{"role": "user", "content": user_msg}],
        )
        raw = response.content[0].text.strip()

        # Strip markdown code fences if the model wrapped the JSON
        if raw.startswith("```"):
            raw = raw.split("```", 1)[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.rsplit("```", 1)[0]

        data: Dict = json.loads(raw.strip())

        # Remove null / empty values so the merger keeps better data from other sources
        cleaned = {k: v for k, v in data.items() if v not in (None, "", [], {})}
        logger.info("AI extraction: %d fields extracted for %r", len(cleaned), company_name)
        return cleaned

    except json.JSONDecodeError as e:
        logger.error("AI extraction returned invalid JSON: %s", e)
        return {}
    except Exception as e:
        logger.error("AI extraction failed: %s", e)
        return {}
