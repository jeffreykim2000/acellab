"""DataValidator — validates fields, detects conflicts, and scores confidence."""
import re
import logging
from dataclasses import asdict
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

CURRENT_YEAR = datetime.now().year

try:
    from src.validators.deliverability_validator import (
        EmailDeliverabilityValidator,
        PhoneDeliverabilityValidator,
        email_confidence_delta,
        phone_confidence_delta,
    )
    _EMAIL_DELIVERABILITY = EmailDeliverabilityValidator()
    _PHONE_DELIVERABILITY = PhoneDeliverabilityValidator()
    HAS_DELIVERABILITY = True
except Exception as _e:
    logger.warning("Deliverability validator unavailable: %s", _e)
    HAS_DELIVERABILITY = False

EMAIL_RE    = re.compile(r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$")
PHONE_RE    = re.compile(r"^\+?[\d\s\-().]{7,25}$")
URL_RE      = re.compile(r"^https?://[^\s/$.?#].[^\s]*$", re.IGNORECASE)
LINKEDIN_RE = re.compile(r"https?://(?:www\.)?linkedin\.com/(company|in)/[\w\-]+/?$", re.IGNORECASE)

# Map each field name to a reliability category used for source weighting
FIELD_CATEGORIES: Dict[str, str] = {
    "name": "identity", "tags": "identity", "introduction": "identity",
    "industry": "identity", "size": "identity", "founded_year": "identity",
    "email": "contact", "phone": "contact", "url": "contact",
    "hq_location": "contact", "address": "contact",
    "funding_rounds": "funding", "investors": "funding", "total_funding": "funding",
    "revenue_range": "funding", "pricing_model": "funding",
    "team_members": "team", "products": "identity",
}

# Fields that should always be present for a usable profile
CRITICAL_FIELDS = {"name", "url", "email", "industry", "hq_location"}

# Source reliability per category (used by both validator and merger)
SOURCE_PRIORITY: Dict[str, List[str]] = {
    "identity": ["linkedin", "crunchbase", "maps", "website", "serp", "news"],
    "contact":  ["maps", "website", "linkedin", "crunchbase", "serp"],
    "funding":  ["crunchbase", "news", "serp", "website"],
    "team":     ["linkedin", "website", "news"],
}


class FieldResult:
    """Single validated field with confidence metadata."""
    def __init__(self, value: Any, source: str, confidence: str, issues: Optional[List[str]] = None):
        self.value = value
        self.source = source
        self.confidence = confidence   # "high" | "medium" | "low"
        self.issues: List[str] = issues or []
        self.needs_review = bool(self.issues)

    def to_dict(self) -> dict:
        return {
            "value": self.value,
            "source": self.source,
            "confidence": self.confidence,
            "issues": self.issues,
            "needs_review": self.needs_review,
        }


class ConflictReport:
    """Describes a disagreement between sources for one field."""
    def __init__(self, field: str, values_by_source: Dict[str, Any], recommended_source: str = ""):
        self.field = field
        self.values_by_source = values_by_source
        self.recommended_source = recommended_source

    def to_dict(self) -> dict:
        return {
            "field": self.field,
            "values_by_source": self.values_by_source,
            "recommended_source": self.recommended_source,
        }


class ValidationReport:
    def __init__(self):
        self.fields: Dict[str, FieldResult] = {}
        self.conflicts: List[ConflictReport] = []
        self.missing_critical: List[str] = []
        self.overall_confidence: float = 0.0

    def to_dict(self) -> dict:
        return {
            "fields": {k: v.to_dict() for k, v in self.fields.items()},
            "conflicts": [c.to_dict() for c in self.conflicts],
            "missing_critical": self.missing_critical,
            "overall_confidence": self.overall_confidence,
        }


class DataValidator:
    """
    Validates individual fields and produces a ValidationReport for a company.
    Used after DataMerger produces the merged dict.
    """

    # ------------------------------------------------------------------ #
    #  Per-field validators — return (ok: bool, issue: str)               #
    # ------------------------------------------------------------------ #

    def validate_email(self, value: str) -> Tuple[bool, str]:
        if not value:
            return False, "Empty"
        v = value.strip()
        if not EMAIL_RE.match(v):
            return False, f"Invalid format: {v}"
        if "example" in v or v.endswith(".test"):
            return False, f"Placeholder email: {v}"
        return True, ""

    def validate_phone(self, value: str) -> Tuple[bool, str]:
        if not value:
            return False, "Empty"
        cleaned = re.sub(r"[\s\-().+]", "", value)
        if not cleaned.isdigit():
            return False, f"Non-digit characters: {value}"
        if not (7 <= len(cleaned) <= 15):
            return False, f"Wrong length ({len(cleaned)} digits): {value}"
        return True, ""

    def validate_url(self, value: str) -> Tuple[bool, str]:
        if not value:
            return False, "Empty"
        try:
            p = urlparse(value.strip())
            if p.scheme not in ("http", "https"):
                return False, f"Non-HTTP scheme: {value}"
            if not p.netloc:
                return False, f"No domain: {value}"
        except Exception:
            return False, f"Unparseable: {value}"
        return True, ""

    def validate_linkedin_url(self, value: str) -> Tuple[bool, str]:
        if not value:
            return False, "Empty"
        if not LINKEDIN_RE.match(value.strip()):
            return False, f"Not a LinkedIn URL: {value}"
        return True, ""

    def validate_founded_year(self, value: Any) -> Tuple[bool, str]:
        try:
            y = int(value)
        except (TypeError, ValueError):
            return False, f"Not a number: {value}"
        if y < 1800:
            return False, f"Implausibly old: {y}"
        if y > CURRENT_YEAR:
            return False, f"Future year: {y}"
        return True, ""

    def validate_funding_amount(self, value: str) -> Tuple[bool, str]:
        if not value:
            return False, "Empty"
        cleaned = re.sub(r"[$,\s]", "", str(value).lower())
        if re.match(r"^\d+(\.\d+)?(m|k|b|million|billion|thousand)?$", cleaned):
            return True, ""
        return False, f"Unrecognised format: {value}"

    # ------------------------------------------------------------------ #
    #  Conflict detection                                                  #
    # ------------------------------------------------------------------ #

    def detect_conflicts(
        self, field: str, values_by_source: Dict[str, Any]
    ) -> Optional[ConflictReport]:
        """Return ConflictReport if sources disagree, else None."""
        non_empty = {s: v for s, v in values_by_source.items() if v or v == 0}
        if len(non_empty) < 2:
            return None
        unique = {str(v).lower().strip() for v in non_empty.values()}
        if len(unique) == 1:
            return None
        category = FIELD_CATEGORIES.get(field, "identity")
        recommended = self._best_source(list(non_empty.keys()), category)
        return ConflictReport(field, non_empty, recommended)

    def _best_source(self, sources: List[str], category: str) -> str:
        priority = SOURCE_PRIORITY.get(category, SOURCE_PRIORITY["identity"])
        for s in priority:
            if s in sources:
                return s
        return sources[0]

    # ------------------------------------------------------------------ #
    #  Confidence scoring                                                  #
    # ------------------------------------------------------------------ #

    def score_confidence(
        self, value: Any, source: str, category: str, issues: List[str]
    ) -> str:
        if not value and value != 0:
            return "low"
        if issues:
            return "low"
        high_sources = {
            "identity": {"linkedin", "crunchbase"},
            "contact":  {"maps", "website"},
            "funding":  {"crunchbase", "news"},
            "team":     {"linkedin"},
        }
        if source in high_sources.get(category, set()):
            return "high"
        return "medium"

    # ------------------------------------------------------------------ #
    #  Full company validation                                             #
    # ------------------------------------------------------------------ #

    def validate_company(
        self, data: Dict[str, Any], source_map: Dict[str, str]
    ) -> ValidationReport:
        """
        Validate every field in a merged company dict.
        source_map maps field name → winning source name.
        Returns a ValidationReport with per-field results and summary stats.
        """
        report = ValidationReport()
        high = medium = total = 0

        validators = {
            "email":         self.validate_email,
            "phone":         self.validate_phone,
            "url":           self.validate_url,
            "founded_year":  self.validate_founded_year,
            "linkedin_url":  self.validate_linkedin_url,
        }

        # Run deliverability checks once upfront
        email_deliv = None
        phone_deliv = None
        if HAS_DELIVERABILITY:
            if data.get("email"):
                try:
                    email_deliv = _EMAIL_DELIVERABILITY.validate_email(data["email"])
                    data["email_deliverability"] = asdict(email_deliv)
                except Exception as _de:
                    logger.warning("Email deliverability check failed: %s", _de)
            if data.get("phone"):
                try:
                    phone_deliv = _PHONE_DELIVERABILITY.validate_phone(data["phone"])
                    data["phone_deliverability"] = asdict(phone_deliv)
                except Exception as _de:
                    logger.warning("Phone deliverability check failed: %s", _de)

        for field, value in data.items():
            if field.startswith("_"):
                continue

            source   = source_map.get(field, "unknown")
            category = FIELD_CATEGORIES.get(field, "identity")
            issues: List[str] = []

            # Run field-specific validator if available
            if field in validators and value:
                ok, msg = validators[field](value)
                if not ok:
                    issues.append(msg)

            # Flag suspicious founding year
            if field == "founded_year" and value:
                ok, msg = self.validate_founded_year(value)
                if not ok:
                    issues.append(msg)

            # Apply deliverability findings to email/phone fields
            if field == "email" and email_deliv:
                issues.extend(email_deliv.issues)
                delta = email_confidence_delta(email_deliv)
                confidence = self.score_confidence(value, source, category, issues)
                # Clamp deliverability-adjusted confidence
                conf_num = {"high": 1.0, "medium": 0.6, "low": 0.2}.get(confidence, 0.2)
                conf_num = max(0.0, min(1.0, conf_num + delta))
                if conf_num >= 0.8:
                    confidence = "high"
                elif conf_num >= 0.4:
                    confidence = "medium"
                else:
                    confidence = "low"
                report.fields[field] = FieldResult(value, source, confidence, issues)
                total += 1
                if confidence == "high":
                    high += 1
                elif confidence == "medium":
                    medium += 1
                continue

            if field == "phone" and phone_deliv:
                delta = phone_confidence_delta(phone_deliv)
                confidence = self.score_confidence(value, source, category, issues)
                conf_num = {"high": 1.0, "medium": 0.6, "low": 0.2}.get(confidence, 0.2)
                conf_num = max(0.0, min(1.0, conf_num + delta))
                if conf_num >= 0.8:
                    confidence = "high"
                elif conf_num >= 0.4:
                    confidence = "medium"
                else:
                    confidence = "low"
                report.fields[field] = FieldResult(value, source, confidence, issues)
                total += 1
                if confidence == "high":
                    high += 1
                elif confidence == "medium":
                    medium += 1
                continue

            confidence = self.score_confidence(value, source, category, issues)
            report.fields[field] = FieldResult(value, source, confidence, issues)
            total += 1
            if confidence == "high":
                high += 1
            elif confidence == "medium":
                medium += 1

        # Flag missing critical fields
        for cf in CRITICAL_FIELDS:
            if not data.get(cf):
                report.missing_critical.append(cf)

        if total:
            report.overall_confidence = round((high + 0.5 * medium) / total, 2)

        logger.info(
            "Validation: %.0f%% confidence, %d missing critical",
            report.overall_confidence * 100, len(report.missing_critical),
        )
        return report
