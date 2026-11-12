"""Versioned, field-level company-research quality benchmark."""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

import startline_agent  # noqa: F401 - exposes agent modules on sys.path
from normalization import entity_key, normalize_value

DATASET_VERSION = 2
MIN_CERTIFICATION_CASES = 50
TERMINAL_STATES = {
    "full_coverage", "complete_with_unpublished_fields",
    "access_blocked", "budget_exhausted",
}
DEFAULT_GATES = {
    "precision": 0.95,
    "recall": 0.80,
    "identity_contact_precision": 0.98,
    "evidence_validity": 1.0,
    "entity_association_accuracy": 0.98,
    "wrong_entity_rate": 0.0,
    "false_completion_rate": 0.0,
    "false_terminal_rate": 0.0,
}


def _norm(field: str, value):
    return normalize_value(field, value)


def _evidence_present(evidence: str, content: str) -> bool:
    norm = lambda value: re.sub(r"\s+", " ", value or "").strip().casefold()
    return bool(evidence and norm(evidence) in norm(content))


def _field_counts(expected: dict, predicted: dict) -> tuple[dict, dict]:
    totals = {field: {"tp": 0, "fp": 0, "fn": 0}
              for field in expected.keys() | predicted.keys()}
    for field, value in predicted.items():
        if field in expected and _norm(field, value) == _norm(field, expected[field]):
            totals[field]["tp"] += 1
        else:
            totals[field]["fp"] += 1
            if field in expected:
                totals[field]["fn"] += 1
    for field in expected.keys() - predicted.keys():
        totals[field]["fn"] += 1
    return totals, {
        "tp": sum(v["tp"] for v in totals.values()),
        "fp": sum(v["fp"] for v in totals.values()),
        "fn": sum(v["fn"] for v in totals.values()),
    }


def _merge_counts(target: dict, incoming: dict) -> None:
    for field, counts in incoming.items():
        bucket = target.setdefault(field, {"tp": 0, "fp": 0, "fn": 0})
        for key in bucket:
            bucket[key] += counts[key]


def _entity_associations(case: dict) -> tuple[int, int]:
    correct = total = 0
    expected_groups = case.get("expected_entities") or {}
    predicted_groups = case.get("predicted_entities") or {}
    for field in expected_groups.keys() | predicted_groups.keys():
        expected = {entity_key(field, item): item
                    for item in expected_groups.get(field, [])}
        for item in predicted_groups.get(field, []):
            total += sum(1 for key, value in item.items()
                         if key not in {"name", "date", "stage", "amount", "currency"}
                         and value not in (None, "", []))
            truth = expected.get(entity_key(field, item))
            if not truth:
                continue
            correct += sum(1 for key, value in item.items()
                           if key not in {"name", "date", "stage", "amount", "currency"}
                           and value not in (None, "", [])
                           and _norm(key, value) == _norm(key, truth.get(key)))
    return correct, total


def extract_offline_fixture(case: dict) -> dict:
    """Run the deterministic web path on an embedded HTML fixture."""
    from deterministic_extractor import extract
    from site_adapter import OfficialSiteAdapter
    html = case.get("source_html") or ""
    source_url = case.get("source_url") or "https://example.test/"
    doc = OfficialSiteAdapter()._to_doc(source_url, html)
    facts = extract(doc.raw_content, doc.source_url, "official_site")
    predicted: dict = {}
    evidence: dict = {}
    entities: dict = {}
    for fact in facts:
        if isinstance(fact.value, list):
            predicted.setdefault(fact.field, []).extend(fact.value)
        else:
            predicted.setdefault(fact.field, fact.value)
        evidence.setdefault(fact.field, fact.evidence)
        if fact.field in {"team_members", "product_entities", "funding_rounds"}:
            entities.setdefault(fact.field, []).extend(fact.value)
    return {**case, "predicted": predicted, "predicted_evidence": evidence,
            "predicted_entities": entities, "source_content": doc.raw_content}


def score_cases(cases: list[dict], gates: dict | None = None) -> dict:
    gates = {**DEFAULT_GATES, **(gates or {})}
    per_field_counts: dict = {}
    tp = fp = fn = wrong_entity = false_complete = false_terminal = 0
    evidence_checked = evidence_valid = association_correct = association_total = 0
    terminal_reasons: dict[str, int] = {}
    for case in cases:
        expected = case.get("expected") or {}
        predicted = case.get("predicted") or {}
        field_counts, overall = _field_counts(expected, predicted)
        _merge_counts(per_field_counts, field_counts)
        tp += overall["tp"]
        fp += overall["fp"]
        fn += overall["fn"]
        if (case.get("expected_entity") and case.get("predicted_entity")
                and _norm("domain", case["expected_entity"]) !=
                _norm("domain", case["predicted_entity"])):
            wrong_entity += 1
        if case.get("reported_complete") and not case.get("actually_exhausted", False):
            false_complete += 1
        predicted_state = case.get("predicted_state")
        if predicted_state:
            terminal_reasons[predicted_state] = terminal_reasons.get(predicted_state, 0) + 1
        expected_outcomes = case.get("expected_outcomes") or {}
        predicted_outcomes = case.get("predicted_outcomes") or {}
        if (predicted_state in TERMINAL_STATES and any(
                predicted_outcomes.get(field) in {None, "researching"}
                or expected_outcomes.get(field) == "researching"
                for field in expected_outcomes)):
            false_terminal += 1
        content = case.get("source_content") or ""
        for field, evidence in (case.get("predicted_evidence") or {}).items():
            if field not in predicted:
                continue
            evidence_checked += 1
            evidence_valid += int(_evidence_present(evidence, content))
        correct, total = _entity_associations(case)
        association_correct += correct
        association_total += total

    precision = tp / (tp + fp) if tp + fp else 1.0
    recall = tp / (tp + fn) if tp + fn else 1.0
    evidence_score = evidence_valid / evidence_checked if evidence_checked else 1.0
    association_score = (association_correct / association_total
                         if association_total else 1.0)
    denominator = len(cases)
    identity_contact = {
        field for field in per_field_counts
        if field in {"legal_name", "brand_name", "domain", "contact_email",
                     "contact_phone", "address", "hq_location"}
    }
    ic_tp = sum(per_field_counts[field]["tp"] for field in identity_contact)
    ic_fp = sum(per_field_counts[field]["fp"] for field in identity_contact)
    metrics = {
        "precision": precision,
        "recall": recall,
        "identity_contact_precision": ic_tp / (ic_tp + ic_fp) if ic_tp + ic_fp else 1.0,
        "evidence_validity": evidence_score,
        "entity_association_accuracy": association_score,
        "wrong_entity_rate": wrong_entity / denominator if denominator else 0.0,
        "false_completion_rate": false_complete / denominator if denominator else 0.0,
        "false_terminal_rate": false_terminal / denominator if denominator else 0.0,
    }
    scalar_gates = {name: threshold for name, threshold in gates.items()
                    if name != "per_field_recall"}
    targets_met = all(metrics[name] >= threshold if name not in {
        "wrong_entity_rate", "false_completion_rate", "false_terminal_rate"}
        else metrics[name] <= threshold for name, threshold in scalar_gates.items())
    per_field = {}
    for field, counts in sorted(per_field_counts.items()):
        field_precision = counts["tp"] / (counts["tp"] + counts["fp"]) \
            if counts["tp"] + counts["fp"] else 1.0
        field_recall = counts["tp"] / (counts["tp"] + counts["fn"]) \
            if counts["tp"] + counts["fn"] else 1.0
        per_field[field] = {**counts, "precision": round(field_precision, 4),
                            "recall": round(field_recall, 4)}
    field_recall_gates = gates.get("per_field_recall") or {}
    field_gate_failures = {
        field: {"actual": per_field.get(field, {}).get("recall", 0.0),
                "required": threshold}
        for field, threshold in field_recall_gates.items()
        if per_field.get(field, {}).get("recall", 0.0) < threshold
    }
    targets_met = targets_met and not field_gate_failures
    rounded = {name: round(value, 4) for name, value in metrics.items()}
    return {
        "dataset_version": DATASET_VERSION,
        "cases": len(cases), "true_positive": tp, "false_positive": fp,
        "false_negative": fn, **rounded,
        "evidence_checked": evidence_checked,
        "entity_associations_checked": association_total,
        "per_field": per_field, "terminal_states": terminal_reasons,
        "release_gates": gates, "field_gate_failures": field_gate_failures,
        "targets_met": targets_met,
        "production_certified": targets_met and len(cases) >= MIN_CERTIFICATION_CASES,
        "certification_note": (
            f"At least {MIN_CERTIFICATION_CASES} human-labelled companies are required."
            if len(cases) < MIN_CERTIFICATION_CASES else "Corpus size requirement met."),
    }


def load_dataset(path: Path, offline_extract: bool = False) -> tuple[list[dict], dict]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, list):
        return payload, {}
    if payload.get("version") != DATASET_VERSION:
        raise ValueError(f"Unsupported benchmark version: {payload.get('version')!r}")
    cases = payload.get("cases") or []
    if offline_extract:
        cases = [extract_offline_fixture(case) if case.get("source_html") else case
                 for case in cases]
    return cases, payload.get("gates") or {}


def regression_alerts(current: dict, baseline: dict, tolerance: float = 0.02) -> list[dict]:
    """Return actionable alerts when quality regresses beyond tolerance."""
    alerts = []
    higher_is_better = ("precision", "recall", "identity_contact_precision",
                        "evidence_validity", "entity_association_accuracy")
    lower_is_better = ("wrong_entity_rate", "false_completion_rate",
                       "false_terminal_rate")
    for metric in higher_is_better:
        if baseline.get(metric, 0) - current.get(metric, 0) > tolerance:
            alerts.append({"metric": metric, "direction": "decreased",
                           "baseline": baseline[metric], "current": current[metric]})
    for metric in lower_is_better:
        if current.get(metric, 0) - baseline.get(metric, 0) > tolerance:
            alerts.append({"metric": metric, "direction": "increased",
                           "baseline": baseline[metric], "current": current[metric]})
    return alerts


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("dataset", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--offline-extract", action="store_true")
    parser.add_argument("--baseline", type=Path)
    args = parser.parse_args()
    cases, gates = load_dataset(args.dataset, args.offline_extract)
    report = score_cases(cases, gates)
    if args.baseline:
        baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
        report["regression_alerts"] = regression_alerts(report, baseline)
    rendered = json.dumps(report, indent=2, ensure_ascii=False)
    if args.output:
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if report["production_certified"] and not report.get("regression_alerts") else 1


if __name__ == "__main__":
    raise SystemExit(main())
