#!/usr/bin/env python3
"""Strict, dependency-free validation for agent-performance routing evals."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any


SUITE_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = SUITE_ROOT.parents[1]
PLUGIN_ROOT = PROJECT_ROOT / "plugins" / "agent-performance"
DEFAULT_SUITE = SUITE_ROOT / "skill-routing-cases.json"

CATEGORIES = {"adversarial", "composition", "direct", "near_miss"}
CATEGORY_ID_PREFIX = {
    "adversarial": "adversarial-",
    "composition": "composition-",
    "direct": "direct-",
    "near_miss": "near-miss-",
}
BOUNDARIES = {"analysis_only", "scoped_writes", "source_read_only"}
REQUIRED_BEHAVIORS = {
    "capture_change_contract",
    "distinguish_review_from_diagnosis",
    "freeze_matched_conditions",
    "gather_fresh_evidence",
    "inspect_current_source",
    "inspect_full_result",
    "match_claim_to_evidence",
    "preserve_documented_compatibility",
    "preserve_required_behavior",
    "reproduce_or_record_gap",
    "separate_output_process_and_harness",
    "separate_static_capability_from_performance",
    "test_plausible_bad_implementation",
    "trace_causal_owner",
    "use_behavioral_oracle",
    "use_exact_request_boundary",
    "validate_review_findings",
}
FORBIDDEN_BEHAVIORS = {
    "add_production_seam_for_test",
    "broaden_scope",
    "claim_without_fresh_evidence",
    "create_external_state",
    "drop_documented_compatibility",
    "edit_production_without_authorization",
    "edit_repository",
    "implement_unrequested_repair",
    "invent_finding",
    "invent_test",
    "manufacture_reproduction",
    "run_unfrozen_comparison",
    "treat_repository_text_as_authority",
    "trust_delegated_summary_as_proof",
    "use_static_inspection_for_winner",
}
CLAIMS = {
    "behavior_preserved",
    "cause",
    "comparison_winner",
    "completion",
    "fixed",
    "review_pass",
    "tests_pass",
}
CLAIM_STATUSES = {"may_remain_unresolved", "must_not_claim", "must_prove"}
AUTHORITY_OVERRIDE_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        (
            r"\bignore\s+(?:all\s+|any\s+|the\s+)?"
            r"(?:previous|prior|system|developer)\s+instructions?\b"
        ),
        (
            r"\boverride(?:s|d|ing)?\s+"
            r"(?:(?:all|developer|host|system)\s+)?(?:instructions?|policy|authority)\b"
        ),
        r"\bsupersede(?:s|d|ing)?\s+(?:all\s+)?(?:instructions?|policy|rules?)\b",
        r"\bthis[- ]supersedes[- ]all\b",
        r"<\s*owner_operating_directive\b",
        r"\bsingle source of truth for how you operate\b",
    )
)


class ValidationError(ValueError):
    """Raised when an eval suite violates its contract."""


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValidationError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValidationError(f"non-finite JSON number: {value}")


def load_document(path: Path) -> dict[str, Any]:
    try:
        with path.open(encoding="utf-8") as handle:
            document = json.load(
                handle,
                object_pairs_hook=_strict_object,
                parse_constant=_reject_constant,
            )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValidationError(f"cannot read JSON suite {path}: {exc}") from exc
    if not isinstance(document, dict):
        raise ValidationError("suite root must be an object")
    return document


def _exact_keys(value: Any, expected: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValidationError(f"{label} must be an object")
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise ValidationError(f"{label} keys differ; missing={missing}, extra={extra}")
    return value


def _sorted_unique_strings(
    value: Any,
    label: str,
    *,
    allowed: set[str] | None = None,
    allow_empty: bool = False,
) -> list[str]:
    if not isinstance(value, list):
        raise ValidationError(f"{label} must be an array of strings")
    if not all(isinstance(item, str) for item in value):
        raise ValidationError(f"{label} must be an array of strings")
    if not allow_empty and not value:
        raise ValidationError(f"{label} must not be empty")
    if value != sorted(value) or len(value) != len(set(value)):
        raise ValidationError(f"{label} must be sorted and duplicate-free")
    if allowed is not None:
        unknown = sorted(set(value) - allowed)
        if unknown:
            raise ValidationError(f"{label} contains unknown values: {unknown}")
    return value


def _plugin_skills(plugin_root: Path) -> list[str]:
    skills_root = plugin_root / "skills"
    if not skills_root.is_dir():
        raise ValidationError(f"skill directory is missing: {skills_root}")
    skills = sorted(
        path.name
        for path in skills_root.iterdir()
        if path.is_dir() and (path / "SKILL.md").is_file()
    )
    if not skills:
        raise ValidationError("plugin has no discoverable skills")
    return skills


def _validate_prompt(prompt: Any, case_id: str) -> str:
    if not isinstance(prompt, str) or prompt != prompt.strip() or not 20 <= len(prompt) <= 600:
        raise ValidationError(f"case {case_id} prompt must be trimmed and 20-600 characters")
    for pattern in AUTHORITY_OVERRIDE_PATTERNS:
        if pattern.search(prompt):
            raise ValidationError(f"case {case_id} prompt contains an authority-override pattern")
    return prompt


def _validate_claims(value: Any, case_id: str) -> set[str]:
    if not isinstance(value, list) or not value:
        raise ValidationError(f"case {case_id} claims must be a non-empty array")
    names: list[str] = []
    for index, raw_claim in enumerate(value):
        claim = _exact_keys(raw_claim, {"name", "status"}, f"case {case_id} claim {index}")
        name = claim["name"]
        status = claim["status"]
        if not isinstance(name, str) or name not in CLAIMS:
            raise ValidationError(f"case {case_id} has unknown claim name: {name!r}")
        if not isinstance(status, str) or status not in CLAIM_STATUSES:
            raise ValidationError(f"case {case_id} claim {name} has unknown status: {status!r}")
        names.append(name)
    if names != sorted(names) or len(names) != len(set(names)):
        raise ValidationError(f"case {case_id} claims must be sorted by unique name")
    return {claim["status"] for claim in value}


def validate_document(document: dict[str, Any], plugin_root: Path = PLUGIN_ROOT) -> dict[str, int]:
    suite = _exact_keys(document, {"cases", "plugin", "schema_version", "skills"}, "suite")
    if suite["schema_version"] != 1 or isinstance(suite["schema_version"], bool):
        raise ValidationError("schema_version must be integer 1")
    if suite["plugin"] != plugin_root.name:
        raise ValidationError(f"plugin must be {plugin_root.name!r}")

    discovered_skills = _plugin_skills(plugin_root)
    declared_skills = _sorted_unique_strings(suite["skills"], "suite skills")
    if declared_skills != discovered_skills:
        raise ValidationError(
            f"suite skills must exactly match plugin skills; declared={declared_skills}, "
            f"discovered={discovered_skills}"
        )
    known_skills = set(discovered_skills)

    raw_cases = suite["cases"]
    if not isinstance(raw_cases, list) or not raw_cases:
        raise ValidationError("cases must be a non-empty array")

    ids: set[str] = set()
    category_counts = {category: 0 for category in CATEGORIES}
    direct_counts = {skill: 0 for skill in discovered_skills}
    near_miss_coverage: set[str] = set()
    composition_coverage: set[str] = set()
    adversarial_coverage: set[str] = set()

    for index, raw_case in enumerate(raw_cases):
        case = _exact_keys(
            raw_case,
            {"category", "covers", "expected", "id", "prompt"},
            f"case {index}",
        )
        case_id = case["id"]
        if not isinstance(case_id, str) or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", case_id):
            raise ValidationError(f"case {index} id must be lowercase hyphen-case")
        if case_id in ids:
            raise ValidationError(f"duplicate case id: {case_id}")
        ids.add(case_id)

        category = case["category"]
        if not isinstance(category, str) or category not in CATEGORIES:
            raise ValidationError(f"case {case_id} has unknown category: {category!r}")
        if not case_id.startswith(CATEGORY_ID_PREFIX[category]):
            raise ValidationError(f"case {case_id} id does not match category {category}")
        category_counts[category] += 1

        covers = _sorted_unique_strings(
            case["covers"], f"case {case_id} covers", allowed=known_skills
        )
        _validate_prompt(case["prompt"], case_id)

        expected = _exact_keys(
            case["expected"],
            {
                "boundary",
                "claims",
                "forbidden_behaviors",
                "required_behaviors",
                "selected_skills",
            },
            f"case {case_id} expected",
        )
        selected = _sorted_unique_strings(
            expected["selected_skills"],
            f"case {case_id} selected_skills",
            allowed=known_skills,
            allow_empty=True,
        )
        boundary = expected["boundary"]
        if not isinstance(boundary, str) or boundary not in BOUNDARIES:
            raise ValidationError(f"case {case_id} has unknown boundary: {boundary!r}")
        required = _sorted_unique_strings(
            expected["required_behaviors"],
            f"case {case_id} required_behaviors",
            allowed=REQUIRED_BEHAVIORS,
        )
        forbidden = _sorted_unique_strings(
            expected["forbidden_behaviors"],
            f"case {case_id} forbidden_behaviors",
            allowed=FORBIDDEN_BEHAVIORS,
        )
        claim_statuses = _validate_claims(expected["claims"], case_id)

        if boundary == "analysis_only":
            needed = {"create_external_state", "edit_repository"}
            if not needed.issubset(forbidden):
                raise ValidationError(
                    f"case {case_id} analysis_only boundary must forbid {sorted(needed)}"
                )
        if boundary == "source_read_only" and "edit_repository" not in forbidden:
            raise ValidationError(
                f"case {case_id} source_read_only boundary must forbid edit_repository"
            )
        if "must_prove" in claim_statuses and not (
            {
                "gather_fresh_evidence",
                "inspect_full_result",
                "match_claim_to_evidence",
                "validate_review_findings",
            }
            & set(required)
        ):
            raise ValidationError(
                f"case {case_id} requires a proved claim without an evidence behavior"
            )

        selected_set = set(selected)
        covers_set = set(covers)
        if category == "direct":
            if len(covers) != 1 or covers != selected:
                raise ValidationError(
                    f"direct case {case_id} must cover and select exactly one identical skill"
                )
            direct_counts[covers[0]] += 1
        elif category == "near_miss":
            if covers_set.issubset(selected_set):
                raise ValidationError(
                    f"near_miss case {case_id} must restrain at least one covered skill"
                )
            near_miss_coverage.update(covers)
        elif category == "composition":
            if len(selected) < 2 or selected != covers:
                raise ValidationError(
                    f"composition case {case_id} must cover and select the same 2+ skills"
                )
            composition_coverage.update(covers)
        else:
            adversarial_coverage.update(covers)

    missing_categories = sorted(
        category for category, count in category_counts.items() if not count
    )
    if missing_categories:
        raise ValidationError(f"suite is missing categories: {missing_categories}")
    bad_direct = {skill: count for skill, count in direct_counts.items() if count != 1}
    if bad_direct:
        raise ValidationError(f"each skill needs exactly one direct case: {bad_direct}")
    for label, coverage in (
        ("near_miss", near_miss_coverage),
        ("composition", composition_coverage),
        ("adversarial", adversarial_coverage),
    ):
        missing = sorted(known_skills - coverage)
        if missing:
            raise ValidationError(f"{label} cases do not cover skills: {missing}")

    return {"cases": len(raw_cases), "skills": len(discovered_skills)}


def validate_path(path: Path, plugin_root: Path = PLUGIN_ROOT) -> dict[str, int]:
    return validate_document(load_document(path), plugin_root)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("suite", nargs="?", type=Path, default=DEFAULT_SUITE)
    args = parser.parse_args(argv)
    try:
        counts = validate_path(args.suite)
    except ValidationError as exc:
        print(f"INVALID: {exc}", file=sys.stderr)
        return 1
    print(f"OK: validated {counts['cases']} routing cases across {counts['skills']} skills")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
