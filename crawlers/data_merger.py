"""DataMerger — merges company data from multiple sources using reliability weights."""
import logging
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Reliability weight per source per category (0 = not useful, 1 = authoritative).
# Adjust these to change which source wins on each field type.
SOURCE_WEIGHTS: Dict[str, Dict[str, float]] = {
    "crunchbase": {"contact": 0.55, "identity": 0.75, "funding": 0.95, "team": 0.60},
    "linkedin":   {"contact": 0.60, "identity": 0.85, "funding": 0.45, "team": 0.90},
    "maps":       {"contact": 0.90, "identity": 0.70, "funding": 0.00, "team": 0.00},
    "website":    {"contact": 0.70, "identity": 0.65, "funding": 0.40, "team": 0.70},
    "serp":       {"contact": 0.50, "identity": 0.60, "funding": 0.60, "team": 0.30},
    "news":       {"contact": 0.30, "identity": 0.50, "funding": 0.80, "team": 0.40},
    "hasdata":    {"contact": 0.75, "identity": 0.70, "funding": 0.70, "team": 0.40},
    "firecrawl":  {"contact": 0.65, "identity": 0.65, "funding": 0.35, "team": 0.60},
    "apify":      {"contact": 0.55, "identity": 0.70, "funding": 0.60, "team": 0.85},
}

FIELD_CATEGORIES: Dict[str, str] = {
    "name": "identity", "tags": "identity", "introduction": "identity",
    "industry": "identity", "size": "identity", "founded_year": "identity",
    "logo_url": "identity", "tagline": "identity",
    "email": "contact", "phone": "contact", "url": "contact",
    "hq_location": "contact", "address": "contact",
    "rating": "contact", "review_count": "contact",
    "funding_rounds": "funding", "investors": "funding", "total_funding": "funding",
    "revenue_range": "funding", "pricing_model": "funding",
    "team_members": "team", "products": "identity",
}


class MergeResult:
    def __init__(self):
        self.merged: Dict[str, Any] = {}
        self.source_map: Dict[str, str] = {}       # field → winning source name
        self.raw: Dict[str, Dict[str, Any]] = {}   # field → {source: value}
        self.conflicts: List[Dict] = []            # fields where sources disagreed
        self.merge_log: List[str] = []

    def to_dict(self) -> dict:
        return {
            "merged":     self.merged,
            "source_map": self.source_map,
            "conflicts":  self.conflicts,
            "merge_log":  self.merge_log,
        }


class DataMerger:
    """
    Merges company data dicts from multiple named sources.

    Usage:
        merger = DataMerger()
        result = merger.merge({
            "hasdata":   {...},
            "firecrawl": {...},
            "apify":     {...},
        })
        print(result.merged)   # best-guess company dict
        print(result.conflicts) # fields where sources disagreed

    Configure source priorities by passing custom_weights:
        merger = DataMerger(custom_weights={"my_source": {"identity": 0.9}})
    """

    def __init__(self, custom_weights: Optional[Dict] = None):
        self._weights = {**SOURCE_WEIGHTS, **(custom_weights or {})}

    def _weight(self, source: str, category: str) -> float:
        return self._weights.get(source, {}).get(category, 0.30)

    _NULL_STRINGS = frozenset({"null", "none", "n/a", "na", "unknown", "undefined", "not available", "-"})

    def _is_empty(self, val: Any) -> bool:
        if val is None or val == "":
            return True
        if isinstance(val, str) and val.lower().strip() in self._NULL_STRINGS:
            return True
        return False

    def _best_scalar(
        self, candidates: Dict[str, Any], category: str
    ) -> Tuple[Any, str]:
        """Return (value, source) for the highest-weight non-empty candidate."""
        best_val, best_src, best_w = None, "unknown", -1.0
        for source, val in candidates.items():
            if self._is_empty(val):
                continue
            w = self._weight(source, category)
            if w > best_w:
                best_w, best_val, best_src = w, val, source
        return best_val, best_src

    def _merge_lists(self, candidates: Dict[str, List]) -> List:
        """Merge list fields by deduplication (preserves order, first-seen wins)."""
        seen: set = set()
        merged: List = []
        for items in candidates.values():
            if not isinstance(items, list):
                continue
            for item in items:
                if isinstance(item, dict):
                    key = str(item.get("name", item)).lower().strip()
                else:
                    key = str(item).lower().strip()
                if key and key not in seen:
                    seen.add(key)
                    merged.append(item)
        return merged

    def merge(self, sources: Dict[str, Dict]) -> MergeResult:
        """
        sources: dict of {source_name: company_data_dict}
        Returns MergeResult with merged data, source attribution, and conflict log.
        """
        result = MergeResult()

        # Collect every field name that appears in any source
        all_fields: set = set()
        for data in sources.values():
            if isinstance(data, dict):
                all_fields.update(data.keys())

        for field in sorted(all_fields):
            if field.startswith("_"):
                continue

            category = FIELD_CATEGORIES.get(field, "identity")
            candidates = {
                src: data.get(field)
                for src, data in sources.items()
                if isinstance(data, dict) and field in data
            }
            result.raw[field] = candidates

            # Determine if this is a list field
            first_val = next((v for v in candidates.values() if v is not None), None)
            if isinstance(first_val, list):
                result.merged[field] = self._merge_lists(candidates)
                result.source_map[field] = "merged"
                result.merge_log.append(f"MERGED_LIST [{field}]")
                continue

            non_empty = {s: v for s, v in candidates.items() if not self._is_empty(v)}
            if not non_empty:
                result.merged[field] = None
                continue

            best_val, best_src = self._best_scalar(non_empty, category)
            result.merged[field] = best_val
            result.source_map[field] = best_src

            # Detect conflicts: more than one unique normalised value
            unique_vals = {str(v).lower().strip() for v in non_empty.values()}
            if len(unique_vals) > 1:
                result.conflicts.append({
                    "field": field,
                    "winner": {"source": best_src, "value": best_val},
                    "all_values": non_empty,
                })
                result.merge_log.append(
                    f"CONFLICT [{field}]: {len(unique_vals)} values → picked {best_src!r}"
                )
            else:
                result.merge_log.append(
                    f"OK [{field}]: {list(non_empty.keys())} agree"
                )

        logger.info(
            "Merge complete: %d fields, %d conflicts",
            len(all_fields), len(result.conflicts),
        )
        return result
